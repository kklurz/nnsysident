import torch

from torch import nn
from nnfabrik.utility.nn_helpers import get_io_dims, get_module_output, set_random_seed, get_dims_for_loader_dict
from collections import OrderedDict, Iterable
import numpy as np
import warnings
from torch.nn import Parameter
from torch.nn import functional as F
from torch.nn import ModuleDict
from mlutils.constraints import positive
from mlutils.layers.cores import DepthSeparableConv2d, Core2d, Stacked2dCore
from mlutils import regularizers
from mlutils.layers.readouts import FullGaussian2d


class MultiReadout:

    def forward(self, *args, data_key=None, **kwargs):
        if data_key is None and len(self) == 1:
            data_key = list(self.keys())[0]
        return self[data_key](*args, **kwargs)

    def regularizer(self, data_key):
        return self[data_key].feature_l1(average=False) * self.gamma_readout


class MultipleFullGaussian2d(MultiReadout, torch.nn.ModuleDict):
    def __init__(self, core, in_shape_dict, n_neurons_dict, init_mu_range, init_sigma, bias, gamma_readout,
                 gauss_type, grid_mean_predictor, grid_mean_predictor_type, source_grids,
                 share_features, share_grid, shared_match_ids):
        # super init to get the _module attribute
        super().__init__()
        k0 = None
        for i, k in enumerate(n_neurons_dict):
            k0 = k0 or k
            in_shape = get_module_output(core, in_shape_dict[k])[1:]
            n_neurons = n_neurons_dict[k]

            source_grid = None
            shared_grid = None
            if grid_mean_predictor is not None:
                if grid_mean_predictor_type == 'cortex':
                    source_grid = source_grids[k]
                else:
                    raise KeyError('grid mean predictor {} does not exist'.format(grid_mean_predictor_type))
            elif share_grid:
                shared_grid = {
                    'match_ids': shared_match_ids[k],
                    'shared_grid': None if i == 0 else self[k0].shared_grid
                }

            if share_features:
                shared_features = {
                    'match_ids': shared_match_ids[k],
                    'shared_features': None if i == 0 else self[k0].shared_features
                }
            else:
                shared_features = None

            self.add_module(k, FullGaussian2d(
                in_shape=in_shape,
                outdims=n_neurons,
                init_mu_range=init_mu_range,
                init_sigma=init_sigma,
                bias=bias,
                gauss_type=gauss_type,
                grid_mean_predictor=grid_mean_predictor,
                shared_features=shared_features,
                shared_grid=shared_grid,
                source_grid=source_grid
            )
                            )
        self.gamma_readout = gamma_readout
