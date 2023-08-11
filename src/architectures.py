from __future__ import annotations

import copy
from pprint import pprint
from typing import Callable, Iterable

import gymnasium as gym
import torch
from jaxtyping import Float, Int
from stable_baselines3.common.policies import ActorCriticPolicy
from stable_baselines3.common.preprocessing import preprocess_obs
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from stable_baselines3.common.utils import obs_as_tensor, get_device
from torch import Tensor
from torch import nn

__all__ = [
    "MLP",
    "L1WeightDecay",
    "SwitchNetwork",
    "SwitchedLayer",
    "CustomActorCriticPolicy",
    "CustomPolicyValueNetwork",
    "NOPFeaturesExtractor",
]


class MLP(nn.Sequential):
    def __init__(self, *dims, add_act_before: bool = False, add_act_after: bool = False,
             activation=nn.Tanh):
        """A multi-layer perceptron.

        Args:
            *dims: The dimensions of the MLP. The first is the input dimension, the last is the output dimension.
            add_act_before: Whether to add an activation before the first layer.
            add_act_after: Whether to add an activation after the last layer.
            activation: The activation function to use.
        """
        layers = [
            layer
            for dim_in, dim_out in zip(dims[:-1], dims[1:])
            for layer in (nn.Linear(dim_in, dim_out), activation())
        ]

        if layers:
            if add_act_before:
                layers.insert(0, activation())

            if not add_act_after:
                layers.pop()

        super().__init__(*layers)


class L1WeightDecay(torch.nn.Module):
    # Adapted from https://github.com/szymonmaszke/torchlayers/blob/master/torchlayers/regularization.py#L150
    def __init__(self, module, weight_decay: float, name: str = None):
        super().__init__()
        assert weight_decay >= 0.0
        self.module = module
        self.weight_decay = weight_decay
        self.name_filter = name

        self.hook = self.module.register_full_backward_hook(self._weight_decay_hook)

    def regularize(self, parameter):
        return parameter.data.sign() * self.weight_decay

    def _weight_decay_hook(self, *_):
        if self.weight_decay <= 0.0:
            return
        if self.name_filter is None:
            for param in self.module.parameters():
                # noinspection PyTypeChecker
                if param.grad is None or torch.all(param.grad == 0.0):
                    param.grad = self.regularize(param)
        else:
            for name, param in self.module.named_parameters():
                # noinspection PyTypeChecker
                if self.name_filter in name and (
                        param.grad is None or torch.all(param.grad == 0.0)
                ):
                    param.grad = self.regularize(param)

    def forward(self, *args, **kwargs):
        return self.module(*args, **kwargs)

    def extra_repr(self) -> str:
        representation = f"weight_decay={self.weight_decay}"
        if self.name_filter is not None:
            representation += f", name_filter={self.name_filter}"
        return representation

    def remove(self):
        self.hook.remove()


# Switch networks


class SwitchedLayer(nn.ModuleList):
    def __init__(self, layer: nn.Module, n_switches: int):
        super().__init__([copy.deepcopy(layer) for _ in range(n_switches)])

    def forward(self, x: Float[Tensor, "batch *dims"], switch: Int[Tensor, "batch"]) -> Tensor:
        # To do the forward pass, through the switch, we need to take into
        # account the batch dimension. We run a forward pass for each switch
        all_x = torch.stack([layer(x) for layer in self], dim=1)
        # and then select the right one.
        x = all_x[torch.arange(len(x)), switch]
        return x


class SwitchNetwork(nn.ModuleList):
    def __init__(self, *layers, switched: int | Iterable[int], n_switches: int):
        super().__init__(layers)
        self.n_switches = n_switches

        if isinstance(switched, int):
            switched = [switched]

        for switched_layer in switched:
            self[switched_layer] = SwitchedLayer(self[switched_layer], n_switches)

    def forward(self, x: Tensor | dict, switch: Int[Tensor, "batch"] = None) -> Tensor:
        if switch is None:
            assert isinstance(x, dict), f"switch is None, but x is not a dict: {x}"
            assert x.keys() >= {"obs", "switch"}, f"Invalid keys: {x.keys()}"
            switch = x["switch"]
            x = x["obs"]

        x: Float[Tensor, "batch *in_dim"]
        switch: Int[Tensor, "batch"]

        for layer in self:
            if isinstance(layer, SwitchedLayer):
                x = layer(x, switch)
            else:
                x = layer(x)
        return x

    def mk_net(self, switch: int) -> nn.Module:
        return nn.Sequential(*[
            layer[switch] if isinstance(layer, SwitchedLayer) else layer
            for layer in self
        ])

    def mk_nets(self) -> list[nn.Module]:
        return [self.mk_net(switch) for switch in range(self.n_switches)]


# mlp = SwitchNetwork(*make_mlp(10, 20, 10), switched=(0, 1), n_switches=2)


# Policy related classes


class NOPFeaturesExtractor(BaseFeaturesExtractor):
    """A feature extractor that does nothing"""
    def __init__(self, observation_space: gym.spaces.MultiBinary):
        # Hack: we need to pass a features_dim > 0, but we don't actually use it.
        # features_dim needs to be an integer,
        # so if someone tries to use features_dim, they will get an explicit error somewhere,
        # because we set it to a float, instead of silently failing.
        # noinspection PyTypeChecker
        super().__init__(observation_space, features_dim=1.42)

    def forward(self, observations: Tensor) -> Tensor:
        """Returns the observation as-is"""
        return observations


class CustomPolicyValueNetwork(nn.Module):
    """
    A pair of networks, one for the policy and one for the value function (which might be the same)
    """

    def __init__(
            self,
            observation_space: gym.Space,
            policy_net: nn.Module,
            value_net: nn.Module = None,
    ):
        super().__init__()

        self.policy_net = policy_net
        self.value_net = value_net

        # IMPORTANT:
        # We need to save output dimensions, it's used by SB3 to create the distributions
        # For this, we compute the output of the network with dummy values
        # and extract the dimension from the result
        device = next(policy_net.parameters()).device
        obs = obs_as_tensor(observation_space.sample(), device)
        policy_out, value_out = self.forward(obs)
        self.latent_dim_pi = policy_out.shape[-1]
        self.latent_dim_vf = value_out.shape[-1]
        print("policy_out", policy_out.shape)
        print("value_out", value_out.shape)

    def forward(self, features) -> tuple[Tensor, Tensor]:
        """
        :return: (th.Tensor, th.Tensor) latent_policy, latent_value of the specified network.
            If all layers are shared, then ``latent_policy == latent_value``
        """
        if self.value_net is None:
            out = self.forward_actor(features)
            return out, out
        return self.forward_actor(features), self.forward_critic(features)

    def forward_actor(self, features: Tensor) -> Tensor:
        """Compute the latent policy"""
        return self.policy_net(features)

    def forward_critic(self, features: Tensor) -> Tensor:
        """Compute the latent value"""
        if self.value_net is None:
            return self.forward_actor(features)
        return self.value_net(features)


class CustomActorCriticPolicy(ActorCriticPolicy):
    """Actor critic policy network to which one can pass an arbitrary network
    for the policy and value function, that takes raw observations as input."""

    def __init__(
            self,
            observation_space: gym.spaces.Space,
            action_space: gym.spaces.Space,
            lr_schedule: Callable[[float], float],
            arch: nn.Module | tuple[nn.Module, nn.Module],
            *args,
            **kwargs,
    ):
        print("SwitchActorCriticPolicy init")
        print("Observation space", observation_space)
        print("Action space", action_space)
        print("Arch kwargs")
        pprint(kwargs)

        if isinstance(arch, nn.Module):
            arch = (arch, None)
        self.arch = arch

        super().__init__(
            observation_space,
            action_space,
            lr_schedule,
            features_extractor_class=NOPFeaturesExtractor,
            # Pass remaining arguments to base class
            *args,
            **kwargs,
        )

    def _build_mlp_extractor(self) -> None:
        self.mlp_extractor = CustomPolicyValueNetwork(
            self.observation_space,
            self.arch[0],
            self.arch[1],
        )


# Convenience methods to access the weights of the network.
#
# @property
# def switch_weights(self) -> Float[Tensor, "switch out_dim in_dim"]:
#     """Returns the weights of the switch layers in one tensor."""
#     return torch.stack([layer.weight for layer in self.policy_net.module.switches]).detach().cpu()
#
# @property
# def switch_biases(self) -> Float[Tensor, "switch out_dim"]:
#     """Returns the biases of the switch layers in one tensor."""
#     return torch.stack([layer.bias for layer in self.policy_net.module.switches]).detach().cpu()
#
# @property
# def weights(self) -> Float[Tensor, "layer out_dim in_dim"]:
#     """Returns the weights of the non-switch layers of the policy network in one tensor."""
#     layers = chain(self.policy_net.module.pre_switch, self.policy_net.module.post_switch)
#     return torch.stack([layer.weight for layer in layers]).detach().cpu()
#
# @property
# def biases(self) -> Float[Tensor, "layer out_dim"]:
#     """Returns the biases of the non-switch layers of the policy network in one tensor."""
#     layers = chain(self.policy_net.module.pre_switch, self.policy_net.module.post_switch)
#     return torch.stack([layer.bias for layer in layers]).detach().cpu()