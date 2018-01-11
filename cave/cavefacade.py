import os
import logging
from typing import Union
from collections import OrderedDict
from contextlib import contextmanager
import typing
import json
import copy

import numpy as np
from pandas import DataFrame

from ConfigSpace import Configuration
from smac.epm.rf_with_instances import RandomForestWithInstances
from smac.optimizer.objective import average_cost
from smac.runhistory.runhistory import RunKey, RunValue, RunHistory
from smac.runhistory.runhistory2epm import RunHistory2EPM4Cost
from smac.scenario.scenario import Scenario
from smac.utils.io.traj_logging import TrajLogger
from smac.utils.io.input_reader import InputReader
from smac.utils.validate import Validator

from pimp.importance.importance import Importance

from cave.html.html_builder import HTMLBuilder
from cave.plot.plotter import Plotter
from cave.smacrun import SMACrun
from cave.analyzer import Analyzer
from cave.utils.helpers import get_cost_dict_for_config

from cave.feature_analysis.feature_analysis import FeatureAnalysis
from cave.plot.algorithm_footprint import AlgorithmFootprint

__author__ = "Joshua Marben"
__copyright__ = "Copyright 2017, ML4AAD"
__license__ = "3-clause BSD"
__maintainer__ = "Joshua Marben"
__email__ = "joshua.marben@neptun.uni-freiburg.de"

@contextmanager
def changedir(newdir):
    """ Helper function to change directory, for example to create a scenario
    from file, where paths to the instance- and feature-files are relative to
    the original SMAC-execution-directory. Same with target algorithms that need
    be executed for validation. """
    olddir = os.getcwd()
    os.chdir(os.path.expanduser(newdir))
    try:
        yield
    finally:
        os.chdir(olddir)

class CAVE(object):
    """
    """

    def __init__(self, folders: typing.List[str], output: str,
                 ta_exec_dir: Union[str, None]=None, missing_data_method: str='epm',
                 max_pimp_samples: int=-1, fanova_pairwise=True):
        """
        Initialize SpySMAC facade to handle analyzing, plotting and building the
        report-page easily. During initialization, the analysis-infrastructure
        is built and the data is validated, meaning the overall best
        incumbent is found and default+incumbent are evaluated for all
        instances for all runs, by default using an EPM.
        The class holds two runhistories:
            self.original_rh -> only contains runs from the actual data
            self.validated_rh -> contains original runs and epm-predictions for
                                 all incumbents
        The analyze()-method performs an analysis and outputs a report.html.

        Arguments
        ---------
        folders: list<strings>
            paths to relevant SMAC runs
        output: string
            output for cave to write results (figures + report)
        ta_exec_dir: string
            execution directory for target algorithm (to find instance.txt, ..)
        missing_data_method: string
            from [validation, epm], how to estimate missing runs
        """
        self.logger = logging.getLogger("cave.cavefacade")
        self.ta_exec_dir = ta_exec_dir

        # Create output if necessary
        self.output = output
        self.logger.info("Saving results to %s", self.output)
        if not os.path.exists(output):
            self.logger.debug("Output-dir %s does not exist, creating", self.output)
            os.makedirs(output)
        if not os.path.exists(os.path.join(self.output, "debug")):
            os.makedirs(os.path.join(self.output, "debug"))
        # Log to file
        logger = logging.getLogger()
        handler = logging.FileHandler(os.path.join(self.output, "debug/debug.log"), "w")
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)

        # Global runhistory combines all actual runs of individual SMAC-runs
        # We save the combined (unvalidated) runhistory to disk, so we can use it later on.
        # We keep the validated runhistory (with as many runs as possible) in
        # memory. The distinction is made to avoid using runs that are
        # only estimated using an EPM for further EPMs or to handle runs
        # validated on different hardware (depending on validation-method).
        self.original_rh = RunHistory(average_cost)
        self.validated_rh = RunHistory(average_cost)

        # Save all relevant SMAC-runs in a list
        self.runs = []
        for folder in folders:
            try:
                self.logger.debug("Collecting data from %s.", folder)
                self.runs.append(SMACrun(folder, ta_exec_dir))
            except Exception as err:
                self.logger.warning("Folder %s could not be loaded, failed "
                                    "with error message: %s", folder, err)
                continue
        if not len(self.runs):
            raise ValueError("None of the specified SMAC-folders could be loaded.")

        # Use scenario of first run for general purposes (expecting they are all the same anyway!)
        self.scenario = self.runs[0].solver.scenario

        # Update global runhistory with all available runhistories
        self.logger.debug("Update original rh with all available rhs!")
        runhistory_fns = [os.path.join(run.folder, "runhistory.json") for run in self.runs]
        for rh_file in runhistory_fns:
            self.original_rh.update_from_json(rh_file, self.scenario.cs)
        self.logger.debug('Combined number of Runhistory data points: %d. '
                          '# Configurations: %d. # Runhistories: %d',
                          len(self.original_rh.data),
                          len(self.original_rh.get_all_configs()),
                          len(runhistory_fns))
        self.original_rh.save_json(os.path.join(self.output, "combined_rh.json"))

        # Validator for a) validating with epm, b) plot over time
        # Initialize without trajectory
        self.validator = Validator(self.scenario, None, None)

        # Estimate missing costs for [def, inc1, inc2, ...]
        self.complete_data(method=missing_data_method)
        self.best_run = min(self.runs, key=lambda run:
                self.validated_rh.get_cost(run.solver.incumbent))

        self.default = self.scenario.cs.get_default_configuration()
        self.incumbent = self.best_run.solver.incumbent

        self.logger.debug("Overall best run: %s, with incumbent: %s",
                          self.best_run.folder, self.incumbent)

        # Following variable determines whether a distinction is made
        # between train and test-instances (e.g. in plotting)
        self.train_test = bool(self.scenario.train_insts != [None] and
                               self.scenario.test_insts != [None])

        self.analyzer = Analyzer(self.original_rh, self.validated_rh,
                                 self.default, self.incumbent, self.train_test,
                                 self.scenario, self.validator, self.output,
                                 max_pimp_samples, fanova_pairwise)

        self.builder = HTMLBuilder(self.output, "SpySMAC")
        # Builder for html-website
        self.website = OrderedDict([])

    def complete_data(self, method="epm"):
        """Complete missing data of runs to be analyzed. Either using validation
        or EPM.
        """
        with changedir(self.ta_exec_dir if self.ta_exec_dir else '.'):
            self.logger.info("Completing data using %s.", method)

            path_for_validated_rhs = os.path.join(self.output, "validated_rhs")
            for run in self.runs:
                self.validator.traj = run.traj
                if method == "validation":
                    # TODO determine # repetitions
                    new_rh = self.validator.validate('def+inc', 'train+test', 1, -1,
                                                     runhistory=self.original_rh)
                elif method == "epm":
                    new_rh = self.validator.validate_epm('def+inc', 'train+test', 1,
                                                         runhistory=self.original_rh)
                else:
                    raise ValueError("Missing data method illegal (%s)",
                                     method)
                self.validator.traj = None  # Avoid usage-mistakes
                self.validated_rh.update(new_rh)

    def analyze(self,
                performance=True, cdf=True, scatter=True, confviz=True,
                param_importance=['forward_selection', 'ablation', 'fanova'],
                feature_analysis=["box_violin", "correlation",
                    "feat_importance", "clustering", "feature_cdf"],
                parallel_coordinates=True, cost_over_time=True,
                algo_footprint=True):
        """Analyze the available data and build HTML-webpage as dict.
        Save webpage in 'self.output/SpySMAC/report.html'.
        Analyzing is performed with the analyzer-instance that is initialized in
        the __init__

        Parameters
        ----------
        performance: bool
            whether to calculate par10-values
        cdf: bool
            whether to plot cdf
        scatter: bool
            whether to plot scatter
        confviz: bool
            whether to perform configuration visualization
        param_importance: List[str]
            containing methods for parameter importance
        feature_analysis: List[str]
            containing methods for feature analysis
        parallel_coordinates: bool
            whether to plot parallel coordinates
        cost_over_time: bool
            whether to plot cost over time
        algo_footprint: bool
            whether to plot algorithm footprints
        """

        # Check arguments
        for p in param_importance:
            if p not in ['forward_selection', 'ablation', 'fanova', 'incneighbor']:
                raise ValueError("%s not a valid option for parameter "
                                 "importance!", p)
        for f in feature_analysis:
            if f not in ["box_violin", "correlation", "importance",
                         "clustering", "feature_cdf"]:
                raise ValueError("%s not a valid option for feature analysis!", f)

        # Start analysis
        overview = self.analyzer.create_overview_table(self.best_run.folder)
        self.website["Meta Data"] = {
                     "table": overview,
                     "tooltip": "Meta data such as number of instances, "
                                "parameters, general configurations..." }

        compare_config = self.analyzer.config_to_html(self.default, self.incumbent)
        self.website["Best configuration"] = {"table": compare_config,
                     "tooltip": "Comparing parameters of default and incumbent. "
                                "Parameters that differ from default to "
                                "incumbent are presented first."}

        ########## PERFORMANCE ANALYSIS
        self.website["Performance Analysis"] = OrderedDict()

        if performance:
            performance_table = self.analyzer.create_performance_table(
                                self.default, self.incumbent)
            self.website["Performance Analysis"]["Performance Table"] = {"table": performance_table}

        if cdf:
            cdf_path = self.analyzer.plot_cdf()
            self.website["Performance Analysis"]["Cumulative distribution function (CDF)"] = {
                     "figure": cdf_path,
                     "tooltip": "Plot default versus incumbent performance "
                                "on a cumulative distribution plot. Uses "
                                "validated data!"}

        if scatter and (self.scenario.train_insts != [[None]]):
            scatter_path = self.analyzer.plot_scatter()
            self.website["Performance Analysis"]["Scatterplot"] = {
                     "figure" : scatter_path,
                     "tooltip": "Plot all evaluated instances on a scatter plot, "
                                "to directly compare performance of incumbent "
                                "and default for each instance. Uses validated "
                                "data! (Left: training data; Right: test data)"}
        elif scatter:
            self.logger.info("Scatter plot desired, but no instances available.")

        # Build report before time-consuming analysis
        self.build_website()

        if algo_footprint:
            algo_footprint_plots = self.analyzer.plot_algorithm_footprint()
            self.website["Performance Analysis"]["Algorithm Footprints"] = OrderedDict()
            for p in algo_footprint_plots:
                self.website["Performance Analysis"]["Algorithm Footprints"][os.path.splitext(os.path.split(p)[1])[0]] = {
                    "figure" : p,
                    "tooltip" : "Footprints as described in Smith-Miles."}


        self.build_website()

        ########### Configurator's behavior
        self.website["Configurator's behavior"] = OrderedDict()

        if confviz and self.scenario.feature_array is not None:
            # Sort runhistories and incs wrt cost
            incumbents = [r.solver.incumbent for r in self.runs]
            runhistories = [r.runhistory for r in self.runs]
            costs = [self.validated_rh.get_cost(i) for i in incumbents]
            costs, incumbents, runhistories = (list(t) for t in
                    zip(*sorted(zip(costs, incumbents, runhistories), key=lambda
                        x: x[0])))

            confviz_script = self.analyzer.plot_confviz(incumbents, runhistories)
            self.website["Configurator's behavior"]["Configuration Visualization"] = {
                    "table" : confviz_script,
                    "tooltip" : "Using PCA to reduce dimensionality of the "
                                "search space  and plot the distribution of "
                                "evaluated configurations. The bigger the dot, "
                                "the more often the configuration was "
                                "evaluated. The colours refer to the predicted "
                                "performance in that part of the search space."}
        elif confviz:
            self.logger.info("Configuration visualization desired, but no "
                             "instance-features available.")

        self.build_website()

        if cost_over_time:
            cost_over_time_path = self.analyzer.plot_cost_over_time(self.best_run.traj, self.validator)
            self.website["Configurator's behavior"]["Cost over time"] = {"figure": cost_over_time_path,
                    "tooltip": "The cost of the incumbent estimated over the "
                               "time. The cost is estimated using an EPM that "
                               "is based on the actual runs."}

        self.build_website()

        self.parameter_importance(ablation='ablation' in param_importance,
                                  fanova='fanova' in param_importance,
                                  forward_selection='forward_selection' in
                                                    param_importance,
                                  incneighbor='incneighbor' in param_importance)

        self.build_website()

        if parallel_coordinates:
            # Should be after parameter importance, if performed.
            n_params = 6
            parallel_path = self.analyzer.plot_parallel_coordinates(n_params)
            self.website["Configurator's behavior"]["Parallel Coordinates"] = {
                         "figure" : parallel_path,
                         "tooltip": "Plot explored range of most important parameters."}

        self.build_website()

        self.feature_analysis(box_violin='box_violin' in feature_analysis,
                              correlation='correlation' in feature_analysis,
                              clustering='clustering' in feature_analysis,
                              importance='importance' in feature_analysis)

        self.logger.info("CAVE finished. Report is located in %s",
                         os.path.join(self.output, 'report.html'))

        self.build_website()


    def parameter_importance(self, ablation=False, fanova=False,
                             forward_selection=False, incneighbor=False):
        """Perform the specified parameter importance procedures. """
        # PARAMETER IMPORTANCE
        if (ablation or forward_selection or fanova or incneighbor):
            self.website["Parameter Importance"] = OrderedDict([("tooltip",
                "Parameter Importance explains the individual importance of the "
                "parameters for the overall performance. Different techniques "
                "are implemented: fANOVA (functional analysis of "
                "variance), ablation and forward selection.")])
        sum_ = 0
        if fanova:
            sum_ += 1
            table, plots, pair_plots = self.analyzer.fanova(self.incumbent)

            self.website["Parameter Importance"]["fANOVA"] = OrderedDict([
                ("tooltip", "fANOVA stands for functional analysis of variance "
                            "and predicts a parameters marginal performance. "
                            "The predicted local neighbourhood of "
                            "a parameters optimized value and "
                            "correlations to other parameters are considered. "
                            "This parameters importance is isolated by predicting "
                            "performance changes that depend on other "
                            "parameters.")])

            self.website["Parameter Importance"]["fANOVA"]["Importance"] = {
                         "table": table}
            # Insert plots (the received plots is a dict, mapping param -> path)
            self.website["Parameter Importance"]["fANOVA"]["Marginals"] = OrderedDict([])
            for param, plot in plots.items():
                self.website["Parameter Importance"]["fANOVA"]["Marginals"][param] = {
                        "figure": plot}
            if pair_plots:
                self.website["Parameter Importance"]["fANOVA"]["PairwiseMarginals"] = OrderedDict([])
                for param, plot in pair_plots.items():
                    self.website["Parameter Importance"]["fANOVA"]["PairwiseMarginals"][param] = {
                        "figure": plot}

        if ablation:
            sum_ += 1
            self.logger.info("Ablation...")
            self.analyzer.parameter_importance("ablation", self.incumbent,
                                               self.output)
            ablationpercentage_path = os.path.join(self.output, "ablationpercentage.png")
            ablationperformance_path = os.path.join(self.output, "ablationperformance.png")
            self.website["Parameter Importance"]["Ablation (percentage)"] = {
                        "figure": ablationpercentage_path}
            self.website["Parameter Importance"]["Ablation (performance)"] = {
                        "figure": ablationperformance_path}

        if forward_selection:
            sum_ += 1
            self.logger.info("Forward Selection...")
            self.analyzer.parameter_importance("forward-selection", self.incumbent,
                                               self.output)
            f_s_barplot_path = os.path.join(self.output, "forward-selection-barplot.png")
            f_s_chng_path = os.path.join(self.output, "forward-selection-chng.png")
            self.website["Parameter Importance"]["Forward Selection (barplot)"] = {
                        "figure": f_s_barplot_path}
            self.website["Parameter Importance"]["Forward Selection (chng)"] = {
                        "figure": f_s_chng_path}

        if incneighbor:
            sum_ += 1
            self.logger.info("Local EPM-predictions around incumbent...")
            plots = self.analyzer.local_epm_plots()
            self.website["Parameter Importance"]["Local EPM around incumbent"] = OrderedDict([])
            for param, plot in plots.items():
                self.website["Parameter Importance"]["Local EPM around incumbent"][param] = {
                    "figure": plot}

        if sum_:
            of = os.path.join(self.output, 'pimp.tex')
            self.logger.info('Creating pimp latex table at %s' % of)
            self.analyzer.pimp.table_for_comparison(self.analyzer.evaluators, of, style='latex')


    def feature_analysis(self, box_violin=False, correlation=False,
                         clustering=False, importance=False):
        if not (box_violin or correlation or clustering or importance):
            self.logger.debug("No feature analysis.")
            return

        # FEATURE ANALYSIS (ASAPY)
        # TODO make the following line prettier
        # TODO feat-names from scenario?
        in_reader = InputReader()
        feat_fn = self.scenario.feature_fn

        if not self.scenario.feature_names:
            with changedir(self.ta_exec_dir if self.ta_exec_dir else '.'):
                if not feat_fn or not os.path.exists(feat_fn):
                    self.logger.warning("Feature Analysis needs valid feature "
                                        "file! Either {} is not a valid "
                                        "filename or features are not saved in "
                                        "the scenario.")
                    self.logger.error("Skipping Feature Analysis.")
                    return
                else:
                    feat_names = in_reader.read_instance_features_file(self.scenario.feature_fn)[0]
        else:
            feat_names = copy.deepcopy(self.scenario.feature_names)

        self.website["Feature Analysis"] = OrderedDict([])

        # feature importance using forward selection
        if importance:
            self.website["Feature Analysis"]["Feature importance"] = OrderedDict()
            imp, plots = self.analyzer.feature_importance()
            imp = DataFrame(data=list(imp.values()), index=list(imp.keys()),
                    columns=["Error"])
            imp = imp.to_html()  # this is a table with the values in html
            self.website["Feature Analysis"]["Feature importance"]["Table"] = {"tooltip":
                         "Feature importance calculated using forward selection.",
                                                            "table": imp}
            for p in plots:
                name = os.path.splitext(os.path.basename(p))[0]
                self.website["Feature Analysis"]["Feature importance"][name] = {"tooltip":
                         "Feature importance calculated using forward selection.",
                         "figure": p}

        # box and violin plots
        if box_violin:
            name_plots = self.analyzer.feature_analysis('box_violin', feat_names)
            self.website["Feature Analysis"]["Violin and box plots"] = OrderedDict({
                "tooltip": "Violin and Box plots to show the distribution of each instance feature. We removed NaN from the data."})
            for plot_tuple in name_plots:
                key = "%s" % (plot_tuple[0])
                self.website["Feature Analysis"]["Violin and box plots"][
                    key] = {"figure": plot_tuple[1]}

        # correlation plot
        if correlation:
            correlation_plot = self.analyzer.feature_analysis('correlation', feat_names)
            if correlation_plot:
                self.website["Feature Analysis"]["Correlation plot"] = {"tooltip": "Correlation based on Pearson product-moment correlation coefficients between all features and clustered with Wards hierarchical clustering approach. Darker fields corresponds to a larger correlation between the features.",
                                                            "figure": correlation_plot}

        # cluster instances in feature space
        if clustering:
            cluster_plot = self.analyzer.feature_analysis('clustering', feat_names)
            self.website["Feature Analysis"]["Clustering"] = {"tooltip": "Clustering instances in 2d; the color encodes the cluster assigned to each cluster. Similar to ISAC, we use a k-means to cluster the instances in the feature space. As pre-processing, we use standard scaling and a PCA to 2 dimensions. To guess the number of clusters, we use the silhouette score on the range of 2 to 12 in the number of clusters",
                                                      "figure": cluster_plot}

        self.build_website()

    def build_website(self):
        self.builder.generate_html(self.website)
