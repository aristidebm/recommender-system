import logging
from types import NoneType

import numpy as np
from enum import Enum
from tqdm import tqdm
from typing import Optional
from collections import defaultdict

from src.algorithms.core import Algorithm
from src.helpers.dataset_indexer import IndexedDatasetWrapper
from src.helpers.state_manager import AlgorithmState
from src.helpers.serial_mapper import SerialUnidirectionalMapper
from src.helpers.predictor import Predictor

logger = logging.getLogger(__name__)


# Centralize this if needed somewhere else
class LearningTargetEnum(str, Enum):
    USER = "user"
    ITEM = "item"

    # No need to cache this method because the caching overhead
    # will compensate the 0(2) of the direct method call.
    @classmethod
    def targets(cls):
        """
        Returns the list of the entries' values
        """
        return [member.value for member in cls]


class AlternatingLeastSquaresState(AlgorithmState):

    # We still need the algorithm in other to do prediction
    def to_predictor(self, als, *args):  # noqa

        def predict(user_ratings_data: list):
            """
            Predict ratings for a user based on user and item factors and biases
            and his historical ratings' data'.

            Args:
                user_ratings_data (list): User's historical ratings' data.

            Returns:
                np.ndarray: Predicted ratings for all items.
            """

            user_factor, user_bias = als.learn_user_bias_and_factor(
                user_id=None, user_ratings_data=user_ratings_data
            )

            # The order of the vectors in the matrix product matters as they have
            # the following shape respectively: (`items_count`, hyper_n_factors)
            # and (hyper_n_factors, 1). Broadcasting is used for the biases' additions
            return np.dot(als.item_factors, user_factor) + user_bias + als.item_biases

        def render(predictions: np.ndarray):
            # TODO: Display the movie instead of their ids
            print("predictions =>", predictions)
            items_ids = np.argsort(predictions)
            print("items_ids =>", items_ids)
            return items_ids

        return Predictor(predict_func=predict, render_func=render)


class AlternatingLeastSquares(Algorithm):
    """
    Alternating Least Squares algorithm. In the design, we assume that an instance of an
    algorithm is a process that is just waiting for data to run. And that process state
    be changed as it is being run. So one needs to instance another algorithm instance
    each time.

    This way of thinking makes the implementation easier than assuming that an algorithm
    instance's states should not change in terms of its extrinsic states (the intrinsic
    states of an algorithm are the hyperparameters), which will require us to expose the
    states change using another pattern and that seems more complex.
    """

    # The client code needs this in other to pull intrinsic params dynamically
    HYPER_PARAMETERS = [
        "hyper_lambda",
        "hyper_gamma",
        "hyper_tau",
        "hyper_n_epochs",
        "hyper_n_factors",
    ]

    AlternatingLeastSquaresError = type(
        "AlternatingLeastSquaresError", (Exception,), {}
    )

    def __init__(
        self,
        hyper_lambda: float = 0.1,
        hyper_gamma: float = 0.01,
        hyper_tau: float = 0.1,
        hyper_n_epochs: int = 10,
        hyper_n_factors: int = 10,
        user_factors: Optional[np.ndarray] = None,
        item_factors: Optional[np.ndarray] = None,
        user_biases: Optional[np.ndarray] = None,
        item_biases: Optional[np.ndarray] = None,
    ):

        assert (
            hyper_lambda
            and hyper_gamma
            and hyper_tau
            and hyper_n_factors
            and hyper_n_epochs
        ), (
            # Serves as a message
            hyper_lambda,
            hyper_gamma,
            hyper_tau,
            hyper_n_factors,
            hyper_n_epochs,
        )

        self.hyper_lambda = hyper_lambda
        self.hyper_gamma = hyper_gamma
        self.hyper_tau = hyper_tau
        self.hyper_n_epochs = hyper_n_epochs
        self.hyper_n_factors = hyper_n_factors

        self.user_factors = user_factors
        self.item_factors = item_factors
        self.user_biases = user_biases
        self.item_biases = item_biases

        self._epochs_loss_train = []
        self._epochs_loss_test = []
        self._epochs_rmse_train = []
        self._epochs_rmse_test = []

        # The two following methods rely on the data (indexed data) that will be
        # passed to the `run` method. And they are used to get a user's id or an
        # item's id if we know the user or the item. We want them to be private.
        self._get_user_id: Optional[defaultdict] = None
        self._get_item_id: Optional[defaultdict] = None

    @staticmethod
    def _validate_dimension_equality(self, *arrays):
        """
        Validates that all provided arrays or sequences are of the same
        type and have matching dimensions.

        Parameters:
            arrays: A variable number of arrays or sequences to compare.

        Raises:
            AlternatingLeastSquaresError: If the arrays have mismatched types, shapes, or lengths.
        """

        _type = type(arrays[0])

        if not all(isinstance(arr, (_type, NoneType)) for arr in arrays):  # Skip None
            raise TypeError("All arrays or sequences must be of the same type.")

        if _type == np.ndarray and (
            not all(arr.shape == arrays[0].shape for arr in arrays if arr is not None)
        ):
            raise TypeError(
                "Arrays have mismatched shapes. Ensure all inputs are ndarrays with the same shape"
            )

        if _type == list and (
            not all(len(arr) == len(arrays[0]) for arr in arrays if arr is not None)
        ):
            raise TypeError(
                "Lists have mismatched lengths. Ensure all inputs are lists with the same length."
            )

    def _validate_factors_and_biases(
        self, user_factors, item_factors, user_biases, item_biases
    ):
        """
        Validates that user and item factors have the same shape,
        and user and item biases have the same shape.
        """
        try:
            self._validate_dimension_equality(user_factors, item_factors)
        except TypeError as exc:
            raise self.AlternatingLeastSquaresError(
                f"Expected user_factors and item_factors to have the same shape, "
                f"but got {user_factors.shape} and {item_factors.shape}."
            ) from exc

        try:
            self._validate_dimension_equality(user_biases, item_biases)
        except TypeError as exc:
            raise self.AlternatingLeastSquaresError(
                f"Expected user_biases and item_biases to have the same shape, "
                f"but got {user_biases.shape} and {item_biases.shape}."
            ) from exc

    def _validate_epochs_losses_and_rmse(
        self, loss_train, loss_test, rmse_train, rmse_test
    ):
        """
        Validates that all epoch-related lists (loss and RMSE) have the same length.
        """
        try:
            self._validate_dimension_equality(
                loss_train, loss_test, rmse_train, rmse_test
            )
        except TypeError as exc:
            raise self.AlternatingLeastSquaresError(
                f"Expected loss_train, loss_test, rmse_train, and rmse_test to have the same length, "
                f"but got lengths {len(loss_train)}, {len(loss_test)}, {len(rmse_train)}, and {len(rmse_test)}."
            ) from exc

    def _load_state(self, state: AlternatingLeastSquaresState):
        """
        Internal method to update the state of the algorithm.
        This is not exposed to client code to ensure encapsulation.
        """
        self.__init__(
            hyper_lambda=state.hyper_lambda,
            hyper_gamma=state.hyper_gamma,
            hyper_tau=state.hyper_tau,
            hyper_n_epochs=state.hyper_n_epochs,
            hyper_n_factors=state.hyper_n_factors,
            user_factors=state.user_factors,
            item_factors=state.item_factors,
            user_biases=state.user_biases,
            item_biases=state.item_biases,
        )

        self._epochs_loss_train = state.loss_train or []
        self._epochs_loss_test = state.loss_test or []
        self._epochs_rmse_train = state.rmse_train or []
        self._epochs_rmse_test = state.rmse_test or []

    @property
    def state(self) -> AlgorithmState:

        return AlternatingLeastSquaresState(
            {
                # Non changing states (intrinsic)
                "hyper_lambda": self.hyper_lambda,
                "hyper_tau": self.hyper_tau,
                "hyper_gamma": self.hyper_gamma,
                "hyper_n_epochs": self.hyper_n_epochs,
                "hyper_n_factors": self.hyper_n_factors,
                # The states that change (extrinsic)
                "user_factors": self.user_factors,
                "item_factors": self.item_factors,
                "user_biases": self.user_biases,
                "item_biases": self.item_biases,
                "loss_train": self._epochs_loss_train,
                "loss_test": self._epochs_loss_test,
                "rmse_train": self._epochs_rmse_train,
                "rmse_test": self._epochs_rmse_test,
            }
        )

    def _validate_state(self, state):
        self._validate_factors_and_biases(
            state.user_factors, state.item_factors, state.user_biases, state.item_biases
        )
        self._validate_epochs_losses_and_rmse(
            state.loss_train, state.loss_test, state.rmse_train, state.rmse_test
        )

    def _finalize_factors_and_biases_initialization(
        self, data_by_user_id__train, data_by_item_id__train
    ):
        """
        Initialize factors and biases based on the information provided while
        creating an instance of the algorithm. Like either learn factors and biases
        or initialize using the respective distributions they are coming from.
        """
        users_count = len(data_by_user_id__train)
        items_count = len(data_by_item_id__train)

        # If we know user factors and user biases but item factors and biases are not known,
        # we can learn them using user factors and user biases that we know. And inversely,
        # if we know item factors and item biases but user factors and biases are unknown, we
        # can learn them too.

        if (self.user_factors is None or self.user_biases is None) and (
            self.item_factors is None or self.item_biases is None
        ):
            logger.info(
                "Initializing user and item's factors and biases, as none of them is provided."
            )
            self.user_factors = self._get_factor_sample(
                size=(users_count, self.hyper_n_factors)
            )
            self.item_factors = self._get_factor_sample(
                size=(items_count, self.hyper_n_factors)
            )
            self.user_biases = self._get_bias_sample(users_count)
            self.item_biases = self._get_bias_sample(items_count)

        elif not (self.user_factors is None or self.user_biases is None):
            # Initialize item factors and biases and then update the factors and biases via learning
            logger.info(
                "Learning item factors and biases using the provided `user_factors` and `user_biases`..."
            )
            self.item_factors = self._get_factor_sample(
                size=(items_count, self.hyper_n_factors)
            )
            self.item_biases = self._get_bias_sample(items_count)
            for item_id in data_by_item_id__train:
                self.update_item_bias_and_factor(
                    item_id, data_by_item_id__train[item_id]
                )
        elif not (self.item_factors is None or self.item_biases is None):
            # Initialize user factors and biases and then update the factors and biases via learning
            logger.info(
                "Learning user factors and biases using the provided `item_factors` and `item_biases`..."
            )

            self.user_factors = self._get_factor_sample(
                size=(users_count, self.hyper_n_factors)
            )

            self.user_biases = self._get_bias_sample(size=users_count)
            for user_id in data_by_user_id__train:
                self.update_user_bias_and_factor(
                    user_id, data_by_user_id__train[user_id]
                )
        else:
            # We have all the factors and biases defined, so nothing to do
            logger.info(
                "All factors and biases are already provided, so no initialization is needed."
            )

    def _learn_bias_and_factor(
        self,
        target: LearningTargetEnum,
        target_id: Optional[int] = None,
        ratings_data: Optional[list] = None,
    ):
        """
        Learn or compute user or item (target) related bias and factor based on the
        provided ratings data and the actual state of the item biases and factors.

        NOTE: In the code, target is either "user" or "item". And we use "target" and
        "other_target" to designate both of them.
        """

        # TODO: Find an elegant way to do this `_target_to_other_target_header` thing
        #  according to whether the `_construct_data`. Because headers need to be dynamic
        #  of the DatasetIndexer will be kept or not.

        _target_to_other_target_header = {
            LearningTargetEnum.USER: "movieId",
            LearningTargetEnum.ITEM: "userId",
        }

        _targets = LearningTargetEnum.targets()

        _mapping = {
            LearningTargetEnum.USER: (
                self.user_factors,
                self.user_biases,
                self._get_user_id,
            ),
            LearningTargetEnum.ITEM: (
                self.item_factors,
                self.item_biases,
                self._get_item_id,
            ),
        }

        _index = _targets.index(target)

        # Get the target factors to attempt to retrieve the old factor from
        # which we want to learn the bias and them the updated version of the
        # factor.
        target_factors, _, _ = _mapping[_targets[_index]]
        (other_target_factors, other_target_biases, _get_other_target_id) = _mapping[
            _targets[1 - _index]
        ]

        bias = 0
        # The old factor that we want to use in other to learn a new one
        factor = (
            target_factors[target_id]
            if target_id
            else self._get_factor_sample(size=self.hyper_n_factors)
        )
        ratings_count = 0
        _A = np.zeros((self.hyper_n_factors, self.hyper_n_factors))
        _B = np.zeros(self.hyper_n_factors)

        for data in ratings_data:
            other_target, rating = (
                data[_target_to_other_target_header[target]],
                data["rating"],
            )
            rating = float(rating)
            other_target_id = _get_other_target_id(other_target)

            bias += (
                rating
                - other_target_biases[other_target_id]
                - np.dot(factor, other_target_factors[other_target_id])
            )
            ratings_count += 1

        bias = (self.hyper_lambda * bias) / (
            self.hyper_lambda * ratings_count + self.hyper_gamma
        )

        for data in ratings_data:
            other_target, rating = (
                data[_target_to_other_target_header[target]],
                data["rating"],
            )
            rating = float(rating)
            other_target_id = _get_other_target_id(other_target)

            _A += np.outer(
                other_target_factors[other_target_id],
                other_target_factors[other_target_id],
            )
            _B += (
                rating - bias - other_target_biases[other_target_id]
            ) * other_target_factors[other_target_id]

        factor = np.linalg.solve(
            self.hyper_lambda * _A + self.hyper_tau * np.eye(self.hyper_n_factors),
            self.hyper_lambda * _B,
        )

        return factor, bias

    def learn_user_bias_and_factor(
        self, user_id: Optional[int] = None, user_ratings_data: Optional[list] = None
    ):
        """
        Learn or compute the given user_id related bias and factor based on the
        provided ratings data and the actual state of the item biases and factors.
        """

        return self._learn_bias_and_factor(
            target=LearningTargetEnum.USER,
            target_id=user_id,
            ratings_data=user_ratings_data,
        )

    def learn_item_bias_and_factor(
        self, item_id: Optional[int] = None, item_ratings_data: Optional[list] = None
    ):
        """
        Learn or compute the given item_id related bias and factor based on the
        provided ratings data and the actual state of the user biases and factors.
        """

        return self._learn_bias_and_factor(
            target=LearningTargetEnum.ITEM,
            target_id=item_id,
            ratings_data=item_ratings_data,
        )

    def _get_factor_sample(self, size) -> np.ndarray:
        """
        Returns a factor sample using a normal distribution 0 as
        mean and `1 / np.sqrt(self.hyper_n_factors)` as scale.
        """
        return np.random.normal(
            loc=0.0,
            scale=1 / np.sqrt(self.hyper_n_factors),
            size=size,
        )

    @staticmethod
    def _get_bias_sample(size) -> np.ndarray:
        """
        Returns a bias sample initialized to zeros with the given size.
        """
        return np.zeros(size)

    @staticmethod
    def _compute_rmse(
        accumulated_squared_residual: float, residuals_count: int
    ) -> float:
        """
        Returns the Root Mean Squared Error
        """
        return np.sqrt(accumulated_squared_residual / residuals_count)

    def _compute_loss(self, accumulated_squared_residual: float) -> float:
        return (
            (-1 / 2 * self.hyper_lambda * accumulated_squared_residual)
            - self.hyper_tau / 2 * sum(self._get_accumulated_factors_product())
            - self.hyper_gamma * sum(self._get_accumulated_squared_biases())
        )

    def _get_accumulated_squared_residual_and_residuals_count(
        self, data_by_user_id: SerialUnidirectionalMapper
    ) -> tuple[float, int]:
        """
        Compute the accumulated squared residuals and their count for the given data.
        """
        accumulated_squared_residuals = 0
        residuals_count = 0
        for user_id in data_by_user_id:
            for data in data_by_user_id[user_id]:
                # TODO: Deal with "movieId", and clarify why only "movieId" is being used
                item, user_item_rating = data["movieId"], data["rating"]
                user_item_rating = float(user_item_rating)
                item_id = self._get_item_id(item)
                accumulated_squared_residuals += (
                    user_item_rating
                    - (
                        self.user_biases[user_id]
                        + self.item_biases[item_id]
                        + np.dot(self.user_factors[user_id], self.item_factors[item_id])
                    )
                ) ** 2

                residuals_count += 1
        return accumulated_squared_residuals, residuals_count

    def _get_accumulated_squared_biases(self):
        return np.sum(self.user_biases**2), np.sum(self.item_biases**2)

    def _get_accumulated_factors_product(self):
        # TODO: Improve this (numpy first)
        # https://mathworld.wolfram.com/FrobeniusNorm.html#:~:text=The%20Frobenius%20norm%2C%20sometimes%20also,considered%20as%20a%20vector%20norm.
        return sum(np.dot(factor, factor) for factor in self.user_factors), sum(
            np.dot(factor, factor) for factor in self.item_factors
        )

    def update_user_bias_and_factor(self, user_id, user_ratings_data: list):
        """
        Side effect method that updates the given user's bias and latent factor
        """
        user_factor, user_bias = self.learn_user_bias_and_factor(
            user_id, user_ratings_data
        )
        self.user_biases[user_id] = user_bias
        self.user_factors[user_id] = user_factor

    def update_item_bias_and_factor(self, item_id, item_ratings_data: list):
        """
        Side effect method that updates the given item's bias and latent factor
        """
        item_factor, item_bias = self.learn_item_bias_and_factor(
            item_id, item_ratings_data
        )
        self.item_biases[item_id] = item_bias
        self.item_factors[item_id] = item_factor

    def run(self, data: IndexedDatasetWrapper, initial_state: AlternatingLeastSquaresState=None):
        """
        Runs the algorithm on the indexed data, `IndexedDatasetWrapper`.
        """

        assert isinstance(
            data, IndexedDatasetWrapper
        ), "The provided `indexed_data` must be an instance of `IndexedDatasetWrapper`."

        data_by_user_id__train = data.data_by_user_id__train
        data_by_item_id__train = data.data_by_item_id__train

        # The validation data, just to compute the loss for the validation data too.
        # But here we're not distinguishing the validation set from the training's one.
        data_by_user_id__test = data.data_by_user_id__test

        # TODO: Needs doc
        # data_by_item_id__test = indexed_data.data_by_item_id__test

        # We want to run the algorithm in resume mode from the backend
        if initial_state:
            state = (
                AlternatingLeastSquaresState(initial_state)
                if isinstance(initial_state, dict)
                else initial_state
            )

            # Run some validation rules
            self._validate_state(state)

            self._load_state(state)

        # Check if factors biases are set, and learn them if applicable
        self._finalize_factors_and_biases_initialization(
            data_by_user_id__train, data_by_item_id__train
        )

        self._get_user_id = lambda user: data.id_to_user_bmap.inverse[user]
        self._get_item_id = lambda item: data.id_to_item_bmap.inverse[item]

        for epoch in tqdm(range(self.hyper_n_epochs), desc="Epochs", unit="epoch"):

            for user_id in data_by_user_id__train:
                self.update_user_bias_and_factor(
                    user_id, data_by_user_id__train[user_id]
                )

            for item_id in data_by_item_id__train:
                self.update_item_bias_and_factor(
                    item_id, data_by_item_id__train[item_id]
                )

            # We've got a new model from the current epoch, so time to compute
            # the metrics for both the training and test set.

            accumulated_squared_residual_train, residuals_count_train = (
                self._get_accumulated_squared_residual_and_residuals_count(
                    data_by_user_id__train
                )
            )

            accumulated_squared_residual_test, residuals_count_test = (
                self._get_accumulated_squared_residual_and_residuals_count(
                    data_by_user_id__test
                )
            )

            loss_train = self._compute_loss(accumulated_squared_residual_train)
            loss_test = self._compute_loss(accumulated_squared_residual_test)

            # We are assuming that this code neither runs in production .i.e is not end client code.
            # If that assumption changes, the following line should be replaced by a proper raise
            # of an exception.
            assert residuals_count_train and residuals_count_test, (
                "None of `residuals_count_train` or `residuals_count_test` should be zero but "
                f"got {residuals_count_train} and {residuals_count_test} respectively for them. "
                "It happens because the data for which that count comes from is empty."
            )

            rmse_train = self._compute_rmse(
                accumulated_squared_residual_train, residuals_count_train
            )
            rmse_test = self._compute_rmse(
                accumulated_squared_residual_test, residuals_count_test
            )

            self._epochs_loss_train.append(loss_train)
            self._epochs_loss_test.append(loss_test)
            self._epochs_rmse_train.append(rmse_train)
            self._epochs_rmse_test.append(rmse_test)

            # Print the information for the current epoch
            tqdm.write(
                f"Epoch {epoch + 1}/{self.hyper_n_epochs}: "
                f"Loss (Train/Test) = {loss_train:.4f}/{loss_test:.4f}, "
                f"RMSE (Train/Test) = {rmse_train:.4f}/{rmse_test:.4f}"
            )
