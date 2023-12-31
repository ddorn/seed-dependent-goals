"""
Wrappers for grid environments.
"""

from __future__ import annotations

import math
from typing import Callable, TypeVar, SupportsFloat, Any, Sequence

import gymnasium as gym
import numpy as np
import pygame
from gymnasium import ObservationWrapper
from gymnasium.core import WrapperObsType, ObsType
from gymnasium.spaces import MultiBinary, MultiDiscrete

import environments as envs

__all__ = [
    "wrap",
    "AddSwitch",
    "ColorBlindWrapper",
    "OneHotColorBlindWrapper",
    "WeightedChannelWrapper",
    "AddTrueGoalToObsFlat",
    "FunctionRewardWrapper",
]

T = TypeVar("T", bound=gym.Env)


def wrap(env: Callable[[], T], *wrappers: Callable[[gym.Env], gym.Env]) -> Callable[[], T]:
    """Wraps a function that returns an environment with the given wrappers.

    The returned function allows to override the default environment with a different one,
    that will be wrapped with the same wrappers.
    """
    def _wrapper(default: T | Callable[[], T] = env):
        if callable(default):
            e = default()
        else:
            e = default
        for w in wrappers:
            e = w(e)
        return e
    return _wrapper


class AddSwitch(ObservationWrapper):
    """
    A wrapper that adds a switch to the observation.

    Input: in=Any
    Output: Dict(obs=in, switch=Discrete(n_switches))

    If in is already a dict, it will be instead be updated with the switch.
    """

    def __init__(self, env: gym.Env,
                 n_switches: int,
                 switch_function: Callable[[gym.Env], int],
                 ):
        super().__init__(env)
        self.n_switches = n_switches
        self.switch_function = switch_function

        in_space = env.observation_space
        if isinstance(in_space, gym.spaces.Dict):
            assert "switch" not in in_space.spaces, in_space
            self.observation_space = gym.spaces.Dict({
                **in_space,
                "switch": gym.spaces.Discrete(n_switches),
            })
        else:
            self.observation_space = gym.spaces.Dict({
                "obs": in_space,
                "switch": gym.spaces.Discrete(n_switches),
            })

    def observation(self, obs: ObsType) -> WrapperObsType:
        if isinstance(obs, dict):
            output = {**obs}
        else:
            output = {"obs": obs}
        output["switch"] = self.switch_function(self.env)
        return output


class BaseBlindWrapper(ObservationWrapper):

    def __init__(self, env: gym.Env,
                 merged_channels: tuple[int, ...] = (0, 1),
                 reward_indistinguishable_goals: bool = False,
                 disabled: bool = False):
        self.disabled = disabled
        self.merge_channels = list(merged_channels)
        self.reward_indistinguishable_goals = reward_indistinguishable_goals

        in_space = env.observation_space
        assert isinstance(in_space, MultiDiscrete), f"{self.__class__.__name__} expected MultiDiscrete, got {in_space}"
        assert len(in_space.shape) == 2, in_space

        super().__init__(env)

    def step(self, action) -> tuple[WrapperObsType, SupportsFloat, bool, bool, dict[str, Any]]:
        observation, reward, terminated, truncated, info = self.env.step(action)
        unwrapped = self.env.unwrapped
        assert isinstance(unwrapped, envs.ThreeGoalsEnv)

        # Update the reward
        if not self.disabled and self.reward_indistinguishable_goals and terminated:
            # We reached a goal, find which one
            idx = unwrapped.goal_positions.index(unwrapped.agent_pos)

            # Update the reward so that if the agent reached
            # a goal indistinguishable from the true goal, it gets a reward of 1 too
            if self.is_indistinguishable_from_true_goal(unwrapped.GOAL_CELLS[idx]):
                reward = 1
                self.unwrapped.last_reward = reward

        return self.observation(observation), reward, terminated, truncated, info

    def is_indistinguishable_from_true_goal(self, goal: envs.Cell) -> bool:
        """Returns whether the given goal is visually the same as the true goal."""
        raise NotImplementedError()


class ColorBlindWrapper(BaseBlindWrapper):
    """
    This wrapper takes a gridworld observation and converts it to a color-blind observation.

    Input: MultiDiscrete((width, height))
    Output: Box((width, height, 3), [0, 1])
    """
    unwrapped: envs.ThreeGoalsEnv

    def __init__(self, env,
                 merged_channels: tuple[int, ...] = (0, 1),
                 reward_indistinguishable_goals: bool = False,
                 reduction: str = "mean",
                 disabled: bool = False):
        super().__init__(env, merged_channels, reward_indistinguishable_goals, disabled)

        self.observation_space = gym.spaces.Box(
            low=0,
            high=1,
            shape=(*env.observation_space.shape, 3),
        )

        # noinspection PyUnresolvedReferences
        self.color_map_full = np.array([
            pygame.Color(cell.color or 0)[:3]
            for cell in env.ALL_CELLS
        ], dtype=self.observation_space.dtype) / 255.0

        assert reduction in ("mean", "max")
        self.color_map_blind = self.color_map_full.copy()
        reduction_funct = {
            "mean": np.mean,
            "max": np.max,
        }[reduction]
        self.color_map_blind[..., self.merge_channels] = reduction_funct(
            self.color_map_blind[..., self.merge_channels],
            axis=-1, keepdims=True)

    @property
    def color_map(self):
        if self.disabled:
            return self.color_map_full
        else:
            return self.color_map_blind

    def is_indistinguishable_from_true_goal(self, goal: envs.Cell) -> bool:
        unwrapped = self.unwrapped
        goal_idx = unwrapped.ALL_CELLS.index(goal)
        true_idx = unwrapped.ALL_CELLS.index(unwrapped.true_goal)

        goal_color = self.color_map[goal_idx]
        true_color = self.color_map[true_idx]
        return np.all(goal_color == true_color)

    def observation(self, obs: np.ndarray):
        return self.color_map[obs]


class OneHotColorBlindWrapper(BaseBlindWrapper):
    """
    A wrapper that takes a gridworld observation and converts it to a "one-hot" color-blind observation.

    Here "one-hot" as quotes because each cell has a one at all the merged channels if one of them was active.

    Input: MultiDiscrete((width, height))
    Output: MultiBinary((width, height, n_cells))
    """

    unwrapped: envs.ThreeGoalsEnv

    def __init__(self, env: gym.Env,
                 merged_channels: tuple[int, ...] = (2, 3),
                 reward_indistinguishable_goals: bool = False,
                 disabled: bool = False):
        assert isinstance(env.unwrapped, envs.ThreeGoalsEnv)
        super().__init__(env, merged_channels, reward_indistinguishable_goals, disabled)

        self.n_cells = len(self.unwrapped.ALL_CELLS)
        self.observation_space = gym.spaces.MultiBinary(n=(*env.observation_space.shape, self.n_cells))

    def is_indistinguishable_from_true_goal(self, goal: envs.Cell) -> bool:
        unwrapped = self.unwrapped
        if goal == unwrapped.true_goal:
            return True
        elif self.disabled:
            return False
        else:
            true_channel = unwrapped.ALL_CELLS.index(unwrapped.true_goal)
            goal_channel = unwrapped.ALL_CELLS.index(goal)
            return true_channel in self.merge_channels and goal_channel in self.merge_channels

    def observation(self, observation: np.ndarray) -> WrapperObsType:
        # Convert to one-hot
        w, h = observation.shape
        one_hot = np.zeros((w, h, self.n_cells), dtype=bool)
        # for x in range(w):
        #     for y in range(h):
        #         one_hot[x, y, observation[x, y]] = True
        # One liner:
        one_hot[np.arange(w)[:, None], np.arange(h), observation] = True

        # Make indistinguishable
        if not self.disabled:
            one_hot[..., self.merge_channels] = one_hot[..., self.merge_channels].any(axis=-1, keepdims=True)

        return one_hot

class WeightedChannelWrapper(ObservationWrapper):
    """
    This wrapper takes a gridworld image and weights each channel differently.

    Input: Box((width, height, n_channels))
    Output: Box((width, height, n_channels))
    """

    def __init__(self, env, weights: Sequence[float], disabled: bool = False):
        super().__init__(env)
        self.disabled = disabled
        obs = env.observation_space
        assert isinstance(obs, gym.spaces.Box), f"{self.__class__.__name__} expected Box, got {obs}"
        assert len(obs.shape) == 3, obs
        assert obs.shape[-1] == len(weights), f"Expected {len(weights)} channels, got {obs.shape[-1]}"

        self.weights = np.array(weights, dtype=obs.dtype)

    def observation(self, obs: np.ndarray):
        if self.disabled:
            return obs
        else:
            return obs * self.weights


class AddTrueGoalToObsFlat(ObservationWrapper):
    """
    Add the goal to the observation and flatten it.

    Input: MultiDiscrete(w, h)
    Output: MultiDiscrete(w * h + 1)

    Input: MultiBinary(shape)
    Output: MultiBinary(prod(shape) + n_goals)

    Input: Box(shape)
    Output: Box(prod(shape) + n_goals)
    """

    def __init__(self, env):
        super().__init__(env)
        in_space = env.observation_space

        unwrapped = env.unwrapped
        assert isinstance(unwrapped, envs.ThreeGoalsEnv)
        self.n_goals = len(unwrapped.GOAL_CELLS)

        if isinstance(in_space, MultiBinary):
            self.goal_is_one_hot = True
            self.observation_space = MultiBinary(math.prod(in_space.shape) + self.n_goals)
        elif isinstance(in_space, MultiDiscrete):
            self.goal_is_one_hot = False
            self.observation_space = MultiDiscrete(math.prod(in_space.shape) + 1)
        elif isinstance(in_space, gym.spaces.Box):
            self.goal_is_one_hot = True
            low = np.concatenate([in_space.low.flatten(), np.zeros((self.n_goals,))], dtype=in_space.dtype)
            high = np.concatenate([in_space.high.flatten(), np.ones((self.n_goals,))], dtype=in_space.dtype)
            self.observation_space = gym.spaces.Box(
                low=low,
                high=high,
                shape=(math.prod(in_space.shape) + self.n_goals,),
                dtype=in_space.dtype,
            )
        else:
            raise ValueError(f"Unknown observation space in {self.__class__.__name__}: {in_space}")

    def observation(self, obs: np.ndarray) -> ObsType:
        flat = obs.flatten()

        if self.goal_is_one_hot:
            to_add = np.zeros((self.n_goals,), dtype=obs.dtype)
            to_add[self.unwrapped.true_goal_idx] = 1
        else:
            to_add = np.array([self.unwrapped.true_goal_idx], dtype=obs.dtype)

        return np.concatenate([flat, to_add])


class FunctionRewardWrapper(gym.RewardWrapper):
    """
    Sets the reward to the output of the given function.
    """

    def __init__(self, env, reward_function: Callable[[gym.Env], float]):
        super().__init__(env)
        self.reward_function = reward_function

    def reward(self, reward: float) -> float:
        return self.reward_function(self.env)