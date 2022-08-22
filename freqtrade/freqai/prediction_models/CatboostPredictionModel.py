import logging
from typing import Any, Dict, Tuple

from catboost import CatBoostRegressor, Pool
from pandas import DataFrame

from freqtrade.freqai.data_kitchen import FreqaiDataKitchen
from freqtrade.freqai.freqai_interface import IFreqaiModel


logger = logging.getLogger(__name__)


class CatboostPredictionModel(IFreqaiModel):
    """
    User created prediction model. The class needs to override three necessary
    functions, predict(), train(), fit(). The class inherits ModelHandler which
    has its own DataHandler where data is held, saved, loaded, and managed.
    """

    def return_values(self, dataframe: DataFrame, dk: FreqaiDataKitchen) -> DataFrame:
        """
        User uses this function to add any additional return values to the dataframe.
        e.g.
        dataframe['volatility'] = dk.volatility_values
        """

        return dataframe

    def make_labels(self, dataframe: DataFrame, dk: FreqaiDataKitchen) -> DataFrame:
        """
        User defines the labels here (target values).
        :params:
        :dataframe: the full dataframe for the present training period
        """

        dataframe["s"] = (
            dataframe["close"]
            .shift(-self.feature_parameters["period"])
            .rolling(self.feature_parameters["period"])
            .mean()
            / dataframe["close"]
            - 1
        )

        return dataframe["s"]

    def train(self, unfiltered_dataframe: DataFrame,
              pair: str, dk: FreqaiDataKitchen) -> Tuple[DataFrame, DataFrame]:
        """
        Filter the training data and train a model to it. Train makes heavy use of the datahkitchen
        for storing, saving, loading, and analyzing the data.
        :params:
        :unfiltered_dataframe: Full dataframe for the current training period
        :metadata: pair metadata from strategy.
        :returns:
        :model: Trained model which can be used to inference (self.predict)
        """

        logger.info('--------------------Starting training '
                    f'{pair} --------------------')

        # unfiltered_labels = self.make_labels(unfiltered_dataframe, dk)
        # filter the features requested by user in the configuration file and elegantly handle NaNs
        features_filtered, labels_filtered = dk.filter_features(
            unfiltered_dataframe,
            dk.training_features_list,
            dk.label_list,
            training_filter=True,
        )

        # split data into train/test data.
        data_dictionary = dk.make_train_test_datasets(features_filtered, labels_filtered)
        dk.fit_labels()  # fit labels to a cauchy distribution so we know what to expect in strategy
        # normalize all data based on train_dataset only
        data_dictionary = dk.normalize_data(data_dictionary)

        # optional additional data cleaning/analysis
        self.data_cleaning_train(dk)

        logger.info(f'Training model on {len(dk.data_dictionary["train_features"].columns)}'
                    ' features')
        logger.info(f'Training model on {len(data_dictionary["train_features"])} data points')

        model = self.fit(data_dictionary)

        logger.info(f'--------------------done training {pair}--------------------')

        return model

    def fit(self, data_dictionary: Dict) -> Any:
        """
        User sets up the training and test data to fit their desired model here
        :params:
        :data_dictionary: the dictionary constructed by DataHandler to hold
        all the training and test data/labels.
        """

        train_data = Pool(
            data=data_dictionary["train_features"],
            label=data_dictionary["train_labels"],
            weight=data_dictionary["train_weights"],
        )

        test_data = Pool(
            data=data_dictionary["test_features"],
            label=data_dictionary["test_labels"],
            weight=data_dictionary["test_weights"],
        )

        model = CatBoostRegressor(
            allow_writing_files=False,
            verbose=100, early_stopping_rounds=400, **self.model_training_parameters
        )
        model.fit(X=train_data, eval_set=test_data)

        return model

    def predict(self, unfiltered_dataframe: DataFrame,
                dk: FreqaiDataKitchen, first: bool = False) -> Tuple[DataFrame, DataFrame]:
        """
        Filter the prediction features data and predict with it.
        :param: unfiltered_dataframe: Full dataframe for the current backtest period.
        :return:
        :pred_df: dataframe containing the predictions
        :do_predict: np.array of 1s and 0s to indicate places where freqai needed to remove
        data (NaNs) or felt uncertain about data (PCA and DI index)
        """

        dk.find_features(unfiltered_dataframe)
        filtered_dataframe, _ = dk.filter_features(
            unfiltered_dataframe, dk.training_features_list, training_filter=False
        )
        filtered_dataframe = dk.normalize_data_from_metadata(filtered_dataframe)
        dk.data_dictionary["prediction_features"] = filtered_dataframe

        # optional additional data cleaning/analysis
        self.data_cleaning_predict(dk, filtered_dataframe)

        predictions = self.model.predict(dk.data_dictionary["prediction_features"])
        pred_df = DataFrame(predictions, columns=dk.label_list)

        for label in dk.label_list:
            pred_df[label] = ((pred_df[label] + 1) *
                              (dk.data["labels_max"][label] -
                               dk.data["labels_min"][label]) / 2) + dk.data["labels_min"][label]

        return (pred_df, dk.do_predict)