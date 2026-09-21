from __future__ import annotations

# Compatibility topology for the public FIDDLE v2.0.0 QTOF checkpoints.
# Architecture provenance:
#   https://github.com/josiehong/FIDDLE
#   release v2.0.0
#   topology derived from the Apache-2.0 upstream implementation.
#
# This module is limited to deterministic checkpoint-compatibility testing.

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils import weight_norm


QTOF_MODEL_CONFIG = {
    "num_add": 6,
    "input_channels": 1,
    "tcn_channels": [32, 32, 64, 128, 256, 512],
    "tcn_dilations": [1, 2, 4, 8, 8, 8],
    "tcn_kernel_sizes": [45, 43, 41, 39, 37, 35],
    "tcn_dropout": 0.2,
    "add_embedding_dim": 4,
    "ce_embedding_dim": 4,
    "mass_embedding_dim": 4,
    "embedding_dim": 512,
    "formula_decoder_layers": [416, 208, 104, 52, 26, 13],
    "mass_decoder_layers": [416, 208, 104, 26, 13, 1],
    "atomnum_decoder_layers": [416, 208, 104, 26, 13, 1],
    "hcnum_decoder_layers": [416, 208, 104, 26, 13, 1],
    "output_dim": 13,
}


class Chomp1d(nn.Module):
    def __init__(self, size: int):
        super().__init__()
        self.size = size

    def forward(self, x):
        return x[:, :, :-self.size].contiguous()


class TemporalBlock(nn.Module):
    def __init__(self, n_in, n_out, kernel, dilation, dropout):
        super().__init__()
        padding = (kernel - 1) * dilation
        self.conv1 = weight_norm(
            nn.Conv1d(n_in, n_out, kernel, stride=1, padding=padding, dilation=dilation)
        )
        self.chomp1 = Chomp1d(padding)
        self.relu1 = nn.ReLU()
        self.dropout1 = nn.Dropout(dropout)
        self.conv2 = weight_norm(
            nn.Conv1d(n_out, n_out, kernel, stride=1, padding=padding, dilation=dilation)
        )
        self.chomp2 = Chomp1d(padding)
        self.relu2 = nn.ReLU()
        self.dropout2 = nn.Dropout(dropout)
        self.net = nn.Sequential(
            self.conv1,
            self.chomp1,
            self.relu1,
            self.dropout1,
            self.conv2,
            self.chomp2,
            self.relu2,
            self.dropout2,
        )
        self.downsample = weight_norm(nn.Conv1d(n_in, n_out, 1)) if n_in != n_out else None
        self.relu = nn.ReLU()

    def forward(self, x):
        out = self.net(x)
        residual = x if self.downsample is None else self.downsample(x)
        return self.relu(out + residual)


class MS2FNetTCN(nn.Module):
    def __init__(self, config=QTOF_MODEL_CONFIG):
        super().__init__()
        env_dim = (
            config["add_embedding_dim"]
            + config["ce_embedding_dim"]
            + config["mass_embedding_dim"]
        )
        layers = []
        channels = config["tcn_channels"]
        for i, out_ch in enumerate(channels):
            in_ch = config["input_channels"] if i == 0 else channels[i - 1]
            layers.append(
                TemporalBlock(
                    in_ch,
                    out_ch,
                    config["tcn_kernel_sizes"][i],
                    config["tcn_dilations"][i],
                    config["tcn_dropout"],
                )
            )
            if i < len(channels) - 1:
                layers.append(nn.MaxPool1d(kernel_size=2, stride=2))
        self.encoder_ms = nn.ModuleList(layers)
        self.embedding_m = weight_norm(nn.Linear(1, config["mass_embedding_dim"]))
        self.embedding_ce = weight_norm(nn.Linear(1, config["ce_embedding_dim"]))
        self.embedding_add = weight_norm(
            nn.Embedding(config["num_add"] + 1, config["add_embedding_dim"])
        )
        self.fc = nn.Sequential(
            weight_norm(nn.Linear(channels[-1] * 2 + env_dim, config["embedding_dim"])),
            nn.ReLU(),
            weight_norm(nn.Linear(config["embedding_dim"], config["embedding_dim"])),
        )
        self.global_pool = nn.AdaptiveAvgPool1d(1)
        self.decoder_formula = self._decoder(config, "formula")
        self.decoder_mass = self._decoder(config, "mass")
        self.decoder_atomnum = self._decoder(config, "atomnum")
        self.decoder_hcnum = self._decoder(config, "hcnum")

    @staticmethod
    def _decoder(config, kind):
        layers = []
        dim = config["embedding_dim"]
        for width in config[f"{kind}_decoder_layers"]:
            layers.extend([weight_norm(nn.Linear(dim, width)), nn.LeakyReLU(0.2)])
            dim = width
        out = config["output_dim"] if kind == "formula" else 1
        layers.extend([nn.Linear(dim, out), nn.LeakyReLU(0.2)])
        return nn.Sequential(*layers)

    def forward(self, x, env):
        if x.ndim == 2:
            x = x.unsqueeze(2)
        x = x.permute(0, 2, 1)
        pooled = []
        for layer in self.encoder_ms:
            x = layer(x)
            if isinstance(layer, TemporalBlock):
                pooled.append(self.global_pool(x).squeeze(-1))
        mass = self.embedding_m(env[:, 0].unsqueeze(1))
        ce = self.embedding_ce(env[:, 1].unsqueeze(1))
        add = self.embedding_add(env[:, 2].int())
        encoded = self.fc(torch.cat(pooled + [mass, ce, add], dim=1))
        return (
            encoded,
            self.decoder_formula(encoded),
            self.decoder_mass(encoded).squeeze(1),
            self.decoder_atomnum(encoded).squeeze(1),
            self.decoder_hcnum(encoded).squeeze(1),
        )


class FormulaEncoder(nn.Module):
    def __init__(self, config=QTOF_MODEL_CONFIG):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(config["output_dim"], 128),
            nn.ReLU(),
            nn.Linear(128, config["embedding_dim"]),
        )

    def forward(self, formula):
        return F.normalize(self.net(formula), dim=1)


class RescoreHead(nn.Module):
    def __init__(self, config=QTOF_MODEL_CONFIG):
        super().__init__()
        dim = config["embedding_dim"]
        self.net = nn.Sequential(
            nn.Linear(dim, 256),
            nn.ReLU(),
            nn.Linear(256, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
        )

    def forward(self, interaction):
        return self.net(interaction).squeeze(1)
