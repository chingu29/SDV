import logging

import numpy as np
import pandas as pd
from copulas.multivariate import GaussianMultivariate

LOGGER = logging.getLogger(__name__)

IGNORED_DICT_KEYS = ['fitted', 'distribution', 'type']


class Modeler:
    """Modeler class.

    The Modeler class applies the CPA algorithm recursively over all the tables
    from the dataset.

    Args:
        metadata (Metadata):
            Dataset Metadata.
        model (type):
            Class of model to use. Defaults to ``copulas.multivariate.GaussianMultivariate``.
        model_kwargs (dict):
            Keyword arguments to pass to the model. Defaults to ``None``.
    """

    def __init__(self, metadata, model=GaussianMultivariate, model_kwargs=None):
        self.models = dict()
        self.metadata = metadata
        self.model = model
        self.model_kwargs = dict() if model_kwargs is None else model_kwargs

    @classmethod
    def _flatten_array(cls, nested, prefix=''):
        """Flatten an array as a dict.

        Args:
            nested (list, numpy.array):
                Iterable to flatten.
            prefix (str):
                Name to append to the array indices. Defaults to ``''``.

        Returns:
            dict:
                Flattened array.
        """
        result = dict()
        for index in range(len(nested)):
            prefix_key = '__'.join([prefix, str(index)]) if len(prefix) else str(index)

            if isinstance(nested[index], (list, np.ndarray)):
                result.update(cls._flatten_array(nested[index], prefix=prefix_key))

            else:
                result[prefix_key] = nested[index]

        return result

    @classmethod
    def _flatten_dict(cls, nested, prefix=''):
        """Flatten a dictionary.

        This method returns a flatten version of a dictionary, concatenating key names with
        double underscores.

        Args:
            nested (dict):
                Original dictionary to flatten.
            prefix (str):
                Prefix to append to key name. Defaults to ``''``.

        Returns:
            dict:
                Flattened dictionary.
        """
        result = dict()

        for key, value in nested.items():
            prefix_key = '__'.join([prefix, str(key)]) if len(prefix) else key

            if key in IGNORED_DICT_KEYS and not isinstance(value, (dict, list)):
                continue

            elif isinstance(value, dict):
                result.update(cls._flatten_dict(value, prefix_key))

            elif isinstance(value, (np.ndarray, list)):
                result.update(cls._flatten_array(value, prefix_key))

            else:
                result[prefix_key] = value

        return result

    @staticmethod
    def _impute(data):
        for column in data:
            column_data = data[column]
            if column_data.dtype in (np.int, np.float):
                fill_value = column_data.mean()
            else:
                fill_value = column_data.mode()[0]

            data[column] = data[column].fillna(fill_value)

        return data

    def _fit_model(self, data):
        """Fit a model to the given data.

        Args:
            data (pandas.DataFrame):
                Data to fit the model to.

        Returns:
            model:
                Instance of ``self.model`` fitted with data.
        """
        data = self._impute(data)
        model = self.model(**self.model_kwargs)
        model.fit(data)

        return model

    def _get_model_dict(self, data):
        """Fit and serialize a model and flatten its parameters into an array.

        Args:
            data (pandas.DataFrame):
                Data to fit the model to.

        Returns:
            dict:
                Flattened parameters for the fitted model.
        """
        model = self._fit_model(data)

        values = list()
        triangle = np.tril(model.covariance)

        for index, row in enumerate(triangle.tolist()):
            values.append(row[:index + 1])

        model.covariance = np.array(values)
        for distribution in model.distribs.values():
            if distribution.std is not None:
                distribution.std = np.log(distribution.std)

        return self._flatten_dict(model.to_dict())

    def _get_extension(self, child_name, child_table, foreign_key):
        """Generate list of extension for child tables.

        Each element of the list is generated for one single children.
        That dataframe should have as ``index.name`` the ``foreign_key`` name, and as index
        it's values.

        The values for a given index are generated by flattening a model fitted with
        the related data to that index in the children table.

        Args:
            parent (str):
                Name of the parent table.
            children (set[str]):
                Names of the children.
            tables (dict):
                Previously processed tables.
        Returns:
            pandas.DataFrame
        """
        extension_rows = list()
        foreign_key_values = child_table[foreign_key].unique()
        child_table = child_table.set_index(foreign_key)
        for foreign_key_value in foreign_key_values:
            child_rows = child_table.loc[[foreign_key_value]]
            num_child_rows = len(child_rows)
            row = self._get_model_dict(child_rows)
            row['child_rows'] = num_child_rows

            row = pd.Series(row)
            row.index = '__' + child_name + '__' + row.index
            extension_rows.append(row)

        return pd.DataFrame(extension_rows, index=foreign_key_values)

    def cpa(self, table_name, tables, foreign_key=None):
        """Run the CPA algorithm over the indicated table and its children.

        Args:
            table_name (str):
                Name of the table to model.
            tables (dict):
                Dict of tables tha have been already modeled.
            foreign_key (str):
                Name of the foreign key that references this table. Used only when applying
                CPA on a child table.

        Returns:
            pandas.DataFrame:
                table data with the extensions created while modeling its children.
        """
        LOGGER.info('Modeling %s', table_name)

        if tables:
            table = tables[table_name]
        else:
            table = self.metadata.load_table(table_name)

        extended = self.metadata.transform(table_name, table)

        primary_key = self.metadata.get_primary_key(table_name)
        if primary_key:
            extended.index = table[primary_key]
            for child_name in self.metadata.get_children(table_name):
                child_key = self.metadata.get_foreign_key(table_name, child_name)
                child_table = self.cpa(child_name, tables, child_key)
                extension = self._get_extension(child_name, child_table, child_key)
                extended = extended.merge(extension, how='left',
                                          right_index=True, left_index=True)
                extended['__' + child_name + '__child_rows'].fillna(0, inplace=True)

        self.models[table_name] = self._fit_model(extended)

        if primary_key:
            extended.reset_index(inplace=True)

        if foreign_key:
            extended[foreign_key] = table[foreign_key]

        return extended

    def model_database(self, tables=None):
        """Run CPA algorithm on all the tables of this dataset.

        Args:
            tables (dict):
                Optional. Dictinary containing the tables of this dataset.
                If not given, the tables will be loaded using the dataset
                metadata specification.
        """
        for table_name in self.metadata.get_tables():
            if not self.metadata.get_parents(table_name):
                self.cpa(table_name, tables)

        LOGGER.info('Modeling Complete')
