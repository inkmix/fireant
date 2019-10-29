from unittest import TestCase

import fireant as f
from fireant.queries.builder import add_hints
from fireant.tests.dataset.mocks import mock_dataset
from fireant.widgets.base import MetricRequiredException
from pypika import (
    MySQLQuery,
    VerticaQuery,
)


class QueryBuilderTests(TestCase):
    def test_widget_is_immutable(self):
        query1 = mock_dataset.query
        query2 = query1.widget(f.ReactTable(mock_dataset.fields.votes))

        self.assertIsNot(query1, query2)

    def test_dimension_is_immutable(self):
        query1 = mock_dataset.query
        query2 = query1.dimension(mock_dataset.fields.timestamp)

        self.assertIsNot(query1, query2)

    def test_filter_is_immutable(self):
        query1 = mock_dataset.query
        query2 = query1.filter(mock_dataset.fields.timestamp == 'ok')

        self.assertIsNot(query1, query2)

    def test_orderby_is_immutable(self):
        query1 = mock_dataset.query
        query2 = query1.orderby(mock_dataset.fields.timestamp)

        self.assertIsNot(query1, query2)

    def test_widgets_returns_the_widgets(self):
        query = mock_dataset.query

        self.assertIs(query.widgets, query._widgets)

    def test_filters_returns_the_filters(self):
        query = mock_dataset.query

        self.assertIs(query.widgets, query._widgets)

    def test_dimensions_returns_the_dimensions(self):
        query = mock_dataset.query

        self.assertIs(query.dimensions, query._dimensions)

    def test_dimensions_returns_the_references(self):
        query = mock_dataset.query

        self.assertIs(query.references, query._references)

    def test_ordersby_returns_the_orders_by(self):
        query = mock_dataset.query

        self.assertIs(query.ordersby, query._orders)


# noinspection SqlDialectInspection,SqlNoDataSourceInspection
class QueryBuilderValidationTests(TestCase):
    maxDiff = None

    def test_highcharts_requires_at_least_one_axis(self):
        with self.assertRaises(MetricRequiredException):
            mock_dataset.query \
                .widget(f.HighCharts()) \
                .dimension(mock_dataset.fields.timestamp) \
                .sql

    def test_ReactTable_requires_at_least_one_metric(self):
        with self.assertRaises(TypeError):
            mock_dataset.query \
                .widget(f.ReactTable())


class QueryHintsTests(TestCase):
    def test_add_hint_to_query_if_supported_by_dialect_and_hint_is_set(self):
        query = VerticaQuery.from_('table').select('*')
        query_hint = add_hints([query], 'test_hint')
        self.assertEqual('SELECT /*+label(test_hint)*/ * FROM "table"', str(query_hint[0]))

    def test_do_not_add_hints_to_query_if_not_supported_by_dialect(self):
        query = MySQLQuery.from_('table').select('*')
        query_hint = add_hints([query], 'test_hint')
        self.assertEqual('SELECT * FROM `table`', str(query_hint[0]))

    def test_do_not_add_hints_to_query_if_no_hint_string_supplied(self):
        query = VerticaQuery.from_('table').select('*')
        query_hint = add_hints([query], hint=None)
        self.assertEqual('SELECT * FROM "table"', str(query_hint[0]))
