import configparser
import inspect
import logging
import os
import typing

import numpy as np
from ConfigSpace.configuration_space import Configuration
from smac.optimizer.objective import average_cost
from smac.runhistory.runhistory import RunHistory, RunKey


def get_timeout(rh, conf, cutoff):
    """Check for timeouts. If multiple runs for an inst/config-pair are
    available, using the median (not the mean: no fractional timeouts)

    Parameters
    ----------
    rh: RunHistory
        runhistory to take runs from
    conf: Configuration
        config to use
    cutoff: int
        to determine timeouts

    Returns
    -------
    timeouts: Dict(str: bool)
        mapping instances to [True, False], where True indicates a timeout
    """
    # TODO Possibly inconsistent: median over timeouts is timeout, but mean over
    # costs is not. Possible?
    if not cutoff:
        return {}
    # Check if config is in runhistory
    conf_id = rh.config_ids[conf]

    timeouts = {}
    runs = rh.get_runs_for_config(conf)
    for run in runs:
        # Averaging over seeds, run = (inst, seed)
        inst, seed = run
        status = rh.data[RunKey(conf_id, inst, seed)].time < cutoff
        if inst in timeouts:
            timeouts[inst].append(status)
        else:
            timeouts[inst] = [status]
    # Use median
    timeouts = {i: np.floor(np.median(timeouts[i])) for i in timeouts.keys()}
    return timeouts


def get_cost_dict_for_config(rh: RunHistory,
                             conf: Configuration,
                             par: int=1,
                             cutoff: typing.Union[float, None]=None):
    """
    Aggregates loss for configuration on evaluated instances over seeds.

    Parameters
    ----------
    rh: RunHistory
        runhistory with data
    conf: Configuration
        configuration to evaluate
    par: int
        par-factor with which to multiply timeouts
    cutoff: float
        cutoff of scenario - used to penalize costs if par != 1

    Returns
    -------
    cost: dict(instance->cost)
        cost per instance (aggregated or as list per seed)
    """
    # Check if config is in runhistory
    conf_id = rh.config_ids[conf]

    # Map instances to seeds in dict
    runs = rh.get_runs_for_config(conf)
    instance_to_seeds = dict()
    for run in runs:
        inst, seed = run
        if inst in instance_to_seeds:
            instance_to_seeds[inst].append(seed)
        else:
            instance_to_seeds[inst] = [seed]

    # Get loss per instance
    instance_costs = {i: [rh.data[RunKey(conf_id, i, s)].cost for s in
                          instance_to_seeds[i]] for i in instance_to_seeds}

    # Aggregate:
    instance_costs = {i: np.mean(instance_costs[i]) for i in instance_costs}

    # TODO: uncomment next line and delete all above after next SMAC dev->master
    # instance_costs = rh.get_instance_costs_for_config(conf)

    if par != 1:
        if cutoff:
            instance_costs = {k: v if v < cutoff else v * par for k, v in instance_costs.items()}
        else:
            raise ValueError("To apply penalization of costs, a cutoff needs to be provided.")

    return instance_costs


def escape_parameter_name(p):
    """Necessary because:
        1. parameters called 'size' or 'origin' might exist in cs
        2. '-' not allowed in bokeh's CDS"""
    return 'p_' + p.replace('-', '_')


def scenario_sanity_check(s, logger):
    """Check scenario for number of train- and test-instances, (duplicate) features and inconsistencies.
    Logs information and raises ValueError if train-features available, but test-features not."""
    train, test, feat = [t for t in s.train_insts if t], [t for t in s.test_insts if t], list(s.feature_dict.keys())
    train_feat, test_feat = [t for t in feat if t in train], [t for t in feat if t in test]
    logger.debug("Instances: train=%d, test=%d, train-features=%d, test-features=%d",
                 len([t for t in train if t]), len([t for t in test if t]), len(train_feat), len(test_feat))
    if (train and train_feat) and (test and not test_feat):
        raise ValueError("Detected train- and test-instances, but only train-features. Either\n  (a) remove train-"
                         "features\n  (b) add test-features or\n  (c) remove test-instances.")

def combine_runhistories(rhs, logger=None):
    """Combine list of given runhistories. interleaving to best approximate execution order"""
    combi_rh = RunHistory(average_cost)
    rh_to_runs = {rh : list(rh.data.items()) for rh in rhs}
    if logger:
        logger.debug("number of elements: " + str({k : len(v) for k, v in rh_to_runs}))
    idx = 0
    while len(rh_to_runs) > 0:
        for rh in list(rh_to_runs.keys()):
            try:
                k, v = rh_to_runs[rh][idx]
                combi_rh.add(rh.ids_config[k.config_id], v.cost, v.time, v.status, k.instance_id, k.seed, v.additional_info)
            except IndexError:
                rh_to_runs.pop(rh)
        idx += 1
    if logger:
        logger.debug("number of elements in individual rhs: " + str({k : len(v) for k, v in rh_to_runs}))
        logger.debug("number of elements in combined rh: " + str(len(combi_rh.data)))
    return combi_rh

def combine_trajectories(trajs, logger=None):
    """Combine trajectories. Trajectories are expected as an iterable of sorted lists, which are increasing in time.
    A trajectory entry is expected as:
    TrajEntry = collections.namedtuple(
                  'TrajEntry', ['train_perf', 'incumbent_id', 'incumbent',
                                'ta_runs', 'ta_time_used', 'wallclock_time'])

    Parameters
    ----------
    trajs: List[List[TrajEntry]]
        trajectories to be combined

    Returns
    -------
    combined_traj: List[TrajEntry]
        combined trajectory
    """
    # flatten list
    flattened_list = [a for b in trajs for a in b]
    # Sort by wallclock-time
    flattened_list.sort(key=lambda traj_entry: traj_entry['wallclock_time'])
    if logger:
        logger.debug("{} trajectories combined to one with {} elements".format(len(trajs), len(flattened_list)))
        #logger.debug(flattened_list)
    # Now add one by one in order of time if better performance than before
    combined_traj = [flattened_list[0]]
    for entry in flattened_list:
        if entry['cost'] < combined_traj[-1]['cost']:
            combined_traj.append(entry)
    return combined_traj

class MissingInstancesError(Exception):
    """Exception indicating that instances are missing."""
    pass

def get_config_origin(c):
    """Return appropriate configuration origin

    Parameters
    ----------
    c: Configuration
        configuration to be examined

    Returns
    -------
    origin: str
        origin of configuration (e.g. "Local", "Random", etc.)
    """
    if not c.origin:
        origin = "Unknown"
    elif c.origin.startswith("Local") or c.origin == 'Model based pick' or "sorted" in c.origin:
        origin = "Acquisition Function"
    elif c.origin.startswith("Random"):
        origin = "Random"
    else:
        logging.getLogger("cave.utils.helpers").debug("Cannot interpret origin: %s", c.origin)
        origin = "Unknown"
    return origin

def check_for_features(scenario):
    features = scenario.feature_dict
    # filter instance features
    train = scenario.train_insts
    test = scenario.test_insts
    train_feats = {k: v for k, v in features.items() if k in train}
    test_feats = {k: v for k, v in features.items() if k in test}
    if not (train_feats or test_feats):
        raise NotApplicable("Could not detect any instances.")

class Deactivated(Exception):
    pass

class NotApplicable(Exception):
    pass

class NotUniqueError(Exception):
    pass

def load_default_options(options=None):
    # Load the configuration file
    own_folder = os.path.realpath(os.path.abspath(os.path.split(inspect.getfile(inspect.currentframe()))[0]))
    default_options = configparser.ConfigParser()
    default_options.read(os.path.join(own_folder, 'default_analysis_options.ini'))

    if options is not None:
        if isinstance(options, str):
            default_options.read_file(options)
        else:
            default_options.read_dict(options)

    return default_options
