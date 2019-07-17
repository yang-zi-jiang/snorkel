import logging
import os
from collections import defaultdict
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
)

import numpy as np
import torch
import torch.nn as nn

from snorkel.analysis.utils import probs_to_preds
from snorkel.classification.data import DictDataLoader
from snorkel.classification.scorer import Scorer
from snorkel.classification.snorkel_config import default_config
from snorkel.classification.utils import move_to_device, recursive_merge_dicts
from snorkel.types import ArrayLike

from .task import Operation, Task

OutputDict = Dict[str, Mapping[Union[str, int], Any]]


class SnorkelClassifier(nn.Module):
    """A classifier built from one or more tasks to support advanced workflows.

    Parameters
    ----------
    tasks
        A list of ``Task``s to build a model from
    name
        The name of the classifier

    Attributes
    ----------
    config
        The config dict containing the settings for this model
    name
        See above
    module_pool
        A dictionary of all modules used by any of the tasks (See Task docstring)
    task_names
        See Task docstring
    task_flows
        See Task docstring
    loss_funcs
        See Task docstring
    output_funcs
        See Task docstring
    scorers
        See Task docstring
    """

    def __init__(
        self, tasks: List[Task], name: Optional[str] = None, **kwargs: Any
    ) -> None:
        super().__init__()
        assert isinstance(default_config["model_config"], dict)
        self.config = recursive_merge_dicts(
            default_config["model_config"], kwargs, misses="insert"
        )
        self.name = name or type(self).__name__

        # Initiate the model attributes
        self.module_pool = nn.ModuleDict()
        self.task_names: Set[str] = set()
        self.task_flows: Dict[str, List[Operation]] = dict()
        self.loss_funcs: Dict[str, Callable[..., torch.Tensor]] = dict()
        self.output_funcs: Dict[str, Callable[..., torch.Tensor]] = dict()
        self.scorers: Dict[str, Scorer] = dict()

        # Build network with given tasks
        self._build_network(tasks)

        logging.info(
            f"Created multi-task model {self.name} that contains "
            f"task(s) {self.task_names}."
        )

        # Move model to specified device
        self._move_to_device()

    def __repr__(self) -> str:
        cls_name = type(self).__name__
        return f"{cls_name}(name={self.name})"

    def _build_network(self, tasks: List[Task]) -> None:
        """Construct the network from a list of ``Task``s by adding them one by one.

        Parameters
        ----------
        tasks
            A list of ``Task``s
        """
        for task in tasks:
            if not isinstance(task, Task):
                raise ValueError(f"Unrecognized task type {task}.")
            if task.name in self.task_names:
                raise ValueError(
                    f"Found duplicate task {task.name}, different task should use "
                    f"different task name."
                )
            self.add_task(task)

    def add_task(self, task: Task) -> None:
        """Add a single task to the network.

        Parameters
        ----------
        task
            A ``Task`` to add
        """
        # Combine module_pool from all tasks
        for key in task.module_pool.keys():
            if key in self.module_pool.keys():
                if self.config["dataparallel"]:
                    task.module_pool[key] = nn.DataParallel(self.module_pool[key])
                else:
                    task.module_pool[key] = self.module_pool[key]
            else:
                if self.config["dataparallel"]:
                    self.module_pool[key] = nn.DataParallel(task.module_pool[key])
                else:
                    self.module_pool[key] = task.module_pool[key]
        # Collect task names
        self.task_names.add(task.name)
        # Collect task flows
        self.task_flows[task.name] = task.task_flow
        # Collect loss functions
        self.loss_funcs[task.name] = task.loss_func
        # Collect output functions
        self.output_funcs[task.name] = task.output_func
        # Collect scorers
        self.scorers[task.name] = task.scorer

        # Move model to specified device
        self._move_to_device()

    def forward(  # type: ignore
        self, X_dict: Dict[str, Any], task_names: Iterable[str]
    ) -> OutputDict:
        """Do a forward pass through the network for all specified tasks.

        Parameters
        ----------
        X_dict
            A dict of data fields
        task_names
            The names of the tasks to execute the forward pass for

        Returns
        -------
        OutputDict
            A dict mapping each operation name to its corresponding output

        Raises
        ------
        TypeError
            If an Operation input has an invalid type
        ValueError
            If a specified Operation failed to execute
        """
        X_dict_moved = move_to_device(X_dict, self.config["device"])

        outputs: OutputDict = {"_input_": X_dict_moved}  # type: ignore

        # Call forward for each task, using cached result if available
        # Each task flow consists of one or more operations that are executed in order
        for task_name in task_names:
            task_flow = self.task_flows[task_name]

            for operation in task_flow:
                if operation.name not in outputs:
                    if operation.inputs:
                        # Feed the inputs the module requested in the reqested order
                        try:
                            inputs = []
                            for op_input in operation.inputs:
                                if isinstance(op_input[1], int):
                                    # The output of the indicated operation has only
                                    # one field; use that as the input to the current op
                                    op_name, field_idx = op_input
                                    inputs.append(outputs[op_name][field_idx])
                                else:  # isinstance(op_input[1], str)
                                    # The output of the indicated operation has a dict
                                    # of fields; extract the designated field by name
                                    op_name, field_key = op_input
                                    inputs.append(outputs[op_name][field_key])
                        except Exception:
                            raise ValueError(f"Unsuccessful operation {operation}.")
                        output = self.module_pool[operation.module_name].forward(
                            *inputs
                        )
                    else:
                        # Feed the entire outputs dict for the module to pull from
                        # TODO: Remove this option (only slice combiner module uses it)
                        output = self.module_pool[operation.module_name].forward(
                            outputs
                        )
                    if not isinstance(output, Mapping):
                        # Make output a singleton list so it becomes a valid Mapping
                        output = [output]
                    outputs[operation.name] = output

        # Note: We return all outputs to enable advanced workflows such as multi-task
        # learning (where we want to calculate loss from multiple head modules on
        # forward passes).
        return outputs

    def calculate_loss(
        self, X_dict: Dict[str, Any], Y_dict: Dict[str, torch.Tensor]
    ) -> Tuple[Dict[str, torch.Tensor], Dict[str, float]]:
        """Calculate the loss for each task and the number of data points contributing.

        Parameters
        ----------
        X_dict
            A dict of data fields
        Y_dict
            A dict from task names to label sets

        Returns
        -------
        Dict[str, torch.Tensor], Dict[str, float]
            A dict of losses by task name and seen examples by task name
        """

        loss_dict = dict()
        count_dict = dict()

        task_names = Y_dict.keys()
        outputs = self.forward(X_dict, task_names)

        # Calculate loss for each task
        for task_name, Y in Y_dict.items():

            # Select the active samples
            if len(Y.size()) == 1:
                active = Y.detach() != 0
            else:
                active = torch.any(Y.detach() != 0, dim=1)

            # Only calculate the loss when active example exists
            if active.any():
                count_dict[task_name] = active.sum().item()

                loss_dict[task_name] = self.loss_funcs[task_name](
                    outputs,
                    move_to_device(Y, self.config["device"]),
                    move_to_device(active, self.config["device"]),
                )

        return loss_dict, count_dict

    @torch.no_grad()
    def _calculate_probs(
        self, X_dict: Dict[str, Any], task_names: Iterable[str]
    ) -> Dict[str, Iterable[torch.Tensor]]:
        """Calculate the probabilities for each task.

        Parameters
        ----------
        X_dict
            A dict of data fields
        task_names
            A list of task names to calculate probabilities for

        Returns
        -------
        Dict[str, Iterable[torch.Tensor]]
            A dictionary mapping task name to probabilities
        """

        self.eval()

        prob_dict = dict()

        outputs = self.forward(X_dict, task_names)

        for task_name in task_names:
            prob_dict[task_name] = self.output_funcs[task_name](outputs).cpu().numpy()

        return prob_dict

    @torch.no_grad()
    def predict(
        self, dataloader: DictDataLoader, return_preds: bool = False
    ) -> Dict[str, Dict[str, torch.Tensor]]:
        """Calculate probabilities, (optionally) predictions, and pull out gold labels.

        Parameters
        ----------
        dataloader
            A DictDataLoader to make predictions for
        return_preds
            If True, include predictions in the return dict (not just probabilities)

        Returns
        -------
        Dict[str, Dict[str, torch.Tensor]]
            A dictionary mapping label type ('golds', 'probs', 'preds') to values
        """
        self.eval()

        gold_dict_list: Dict[str, List[torch.Tensor]] = defaultdict(list)
        prob_dict_list: Dict[str, List[torch.Tensor]] = defaultdict(list)

        for batch_num, (X_batch_dict, Y_batch_dict) in enumerate(dataloader):
            prob_batch_dict = self._calculate_probs(X_batch_dict, Y_batch_dict.keys())
            for task_name, Y in Y_batch_dict.items():
                prob_dict_list[task_name].extend(prob_batch_dict[task_name])
                gold_dict_list[task_name].extend(Y.cpu().numpy())

        gold_dict: Dict[str, np.ndarray] = {}
        prob_dict: Dict[str, np.ndarray] = {}

        for task_name in gold_dict_list:
            gold_dict[task_name] = np.array(gold_dict_list[task_name])
            prob_dict[task_name] = np.array(prob_dict_list[task_name])

        if return_preds:
            pred_dict: Dict[str, ArrayLike] = defaultdict(list)
            for task_name, probs in prob_dict.items():
                pred_dict[task_name] = probs_to_preds(probs)

        results = {"golds": gold_dict, "probs": prob_dict}

        if return_preds:
            results["preds"] = pred_dict

        return results

    @torch.no_grad()
    def score(self, dataloaders: List["DictDataLoader"]) -> Dict[str, float]:
        """Calculate scores for the provided DictDataLoaders.

        Parameters
        ----------
        dataloaders
            A list of DictDataLoaders to calculate scores for

        Returns
        -------
        Dict[str, float]
            A dictionary mapping metric names to corresponding scores
            Metric names will be of the form "task/dataset/split/metric"
        """

        self.eval()

        metric_score_dict = dict()

        for dataloader in dataloaders:
            results = self.predict(dataloader, return_preds=True)
            for task_name in results["golds"].keys():
                metric_scores = self.scorers[task_name].score(
                    results["golds"][task_name],
                    results["preds"][task_name],
                    results["probs"][task_name],
                )
                for metric_name, metric_value in metric_scores.items():
                    # Type ignore statements are necessary because the DataLoader class
                    # that DictDataLoader inherits from is what actually sets
                    # the class of Dataset, and it doesn't know about name and split.
                    identifier = "/".join(
                        [
                            task_name,
                            dataloader.dataset.name,  # type: ignore
                            dataloader.dataset.split,  # type: ignore
                            metric_name,
                        ]
                    )
                    metric_score_dict[identifier] = metric_value

        return metric_score_dict

    def _move_to_device(self) -> None:  # pragma: no cover
        """Move the model to the device specified in the model config."""
        device = self.config["device"]
        if device >= 0:
            if torch.cuda.is_available():
                logging.info(f"Moving model to GPU (cuda:{device}).")
                self.to(torch.device(f"cuda:{device}"))
            else:
                logging.info("No cuda device available. Switch to cpu instead.")

    def save(self, model_path: str) -> None:
        """Save the model to the specified file path.

        Parameters
        ----------
        model_path
            The path where the model should be saved

        Raises
        ------
        BaseException
            If the torch.save() method fails
        """
        if not os.path.exists(os.path.dirname(model_path)):
            os.makedirs(os.path.dirname(model_path))

        try:
            torch.save(self.state_dict(), model_path)
        except BaseException:  # pragma: no cover
            logging.warning("Saving failed... continuing anyway.")

        logging.info(f"[{self.name}] Model saved in {model_path}")

    def load(self, model_path: str) -> None:
        """Load a saved model from the provided file path and moves it to a device.

        Parameters
        ----------
        model_path
            The path to a saved model
        """
        try:
            self.load_state_dict(
                torch.load(model_path, map_location=torch.device("cpu"))
            )
        except BaseException:  # pragma: no cover
            if not os.path.exists(model_path):
                logging.error("Loading failed... Model does not exist.")
            else:
                logging.error(f"Loading failed... Cannot load model from {model_path}")
            raise

        logging.info(f"[{self.name}] Model loaded from {model_path}")
        self._move_to_device()