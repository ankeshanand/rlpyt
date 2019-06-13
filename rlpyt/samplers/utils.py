
import multiprocessing as mp
import ctypes
import numpy as np

from rlpyt.utils.buffer import buffer_from_example, torchify_buffer
from rlpyt.utils.collections import AttrDict
from rlpyt.agents.base import AgentInputs
from rlpyt.samplers.collections import (Samples, AgentSamples, AgentSamplesBsv,
    EnvSamples, StepBuffer)


def build_samples_buffer(agent, env, batch_spec, bootstrap_value=False,
        agent_shared=True, env_shared=True, subprocess=True):
    """Recommended to step/reset agent and env in subprocess, so it doesn't
    affect settings in master before forking workers (e.g. torch num_threads
    (MKL) may be set at first forward computation.)"""
    if subprocess:
        mgr = mp.Manager()
        examples = mgr.dict()  # Examples pickled back to master.
        w = mp.Process(target=get_example_outputs, args=(agent, env, examples))
        w.start()
        w.join()
    else:
        examples = get_example_outputs(agent, env, dict())

    T, B = batch_spec
    all_action = buffer_from_example(examples["action"], (T + 1, B), agent_shared)
    action = all_action[1:]
    prev_action = all_action[:-1]  # Writing to action will populate prev_action.
    agent_info = buffer_from_example(examples["agent_info"], (T, B), agent_shared)
    agent_buffer = AgentSamples(
        action=action,
        prev_action=prev_action,
        agent_info=agent_info,
    )
    if bootstrap_value:
        bv = buffer_from_example(examples["agent_info"].value, (1, B), agent_shared)
        agent_buffer = AgentSamplesBsv(*agent_buffer, bootstrap_value=bv)

    observation = buffer_from_example(examples["observation"], (T, B), env_shared)
    all_reward = buffer_from_example(examples["reward"], (T + 1, B), env_shared)
    reward = all_reward[1:]
    prev_reward = all_reward[:-1]  # Writing to reward will populate prev_reward.
    done = buffer_from_example(examples["done"], (T, B), env_shared)
    env_info = buffer_from_example(examples["env_info"], (T, B), env_shared)
    env_buffer = EnvSamples(
        observation=observation,
        reward=reward,
        prev_reward=prev_reward,
        done=done,
        env_info=env_info,
    )
    samples_np = Samples(agent=agent_buffer, env=env_buffer)
    samples_pyt = torchify_buffer(samples_np)
    return samples_pyt, samples_np, examples


def build_step_buffer(examples, B):
    bufs = tuple(buffer_from_example(examples[k], B, shared_memory=True)
        for k in ["observation", "action", "reward", "done", "agent_info"])
    need_reset = buffer_from_example(examples["done"], B, shared_memory=True)
    step_buffer_np = StepBuffer(*bufs, need_reset)
    step_buffer_pyt = torchify_buffer(step_buffer_np)
    return step_buffer_pyt, step_buffer_np


def build_par_objs(n, groups=1):
    ctrl = AttrDict(
        quit=mp.RawValue(ctypes.c_bool, False),
        barrier_in=mp.Barrier(n * groups + 1),
        barrier_out=mp.Barrier(n * groups + 1),
        do_eval=mp.RawValue(ctypes.c_bool, False),
        itr=mp.RawValue(ctypes.c_long, 0),
    )
    traj_infos_queue = mp.Queue()

    step_blockers = [[mp.Semaphore(0) for _ in range(n)] for _ in range(groups)]
    act_waiters = [[mp.Semaphore(0) for _ in range(n)] for _ in range(groups)]
    if groups == 1:
        step_blockers = step_blockers[0]
        act_waiters = act_waiters[0]
    sync = AttrDict(
        step_blockers=step_blockers,
        act_waiters=act_waiters,
        do_reset=mp.RawValue(ctypes.c_bool, False),
        stop_eval=mp.RawValue(ctypes.c_bool, False),
    )
    return ctrl, traj_infos_queue, sync
    return ctrl, traj_infos_queue


def get_example_outputs(agent, env, examples):
    """Do this in a sub-process to avoid setup conflict in master/workers (e.g.
    MKL)."""
    print("GETTING EXAMPLE OUTPUTS")
    o = env.reset()
    print("RESET ENV")
    a = env.action_space.sample()
    print("SAMPLE ACTION SPACE")
    o, r, d, env_info = env.step(a)
    r = np.asarray(r, dtype="float32")  # Must match torch float dtype here.
    agent.reset()
    agent_inputs = torchify_buffer(AgentInputs(o, a, r))
    print("TRYING TO STEP AGENT")
    a, agent_info = agent.step(*agent_inputs)
    examples["observation"] = o
    examples["reward"] = r
    examples["done"] = d
    examples["env_info"] = env_info
    examples["action"] = a  # OK to put torch tensor here, could numpify.
    examples["agent_info"] = agent_info
    print("FINISHED EXAMPLE OUTPUTS")
