import copy
from typing import List

from fireant.dataset.fields import Field
from fireant.queries.builder.dataset_query_builder import DataSetQueryBuilder
from fireant.queries.finders import (
    find_dataset_fields,
    find_field_in_modified_field,
    find_metrics_for_widgets,
    find_share_dimensions,
    find_operations_for_widgets,
    find_share_operations)
from fireant.queries.sql_transformer import make_slicer_query_with_totals_and_references
from fireant.reference_helpers import reference_alias
from fireant.utils import (
    alias_selector,
    listify,
    ordered_distinct_list_by_attr)
from fireant.widgets.base import Widget
from pypika import Query


@listify
def _find_dataset_fields_needed_to_be_mapped(dataset):
    """
    This produces a list of fields from a DatasetBlender that need to be mapped up to the Datasets. This is any simple
    fields, or fields that are just pointers to fields in a Dataset, and breaks up complex fields, fields that are
    defined with expressions referencing simple fields from the datasets, down to those fields.

    Blender
        primary
            DS0
                Fields
                    dimension0
                    metric0
        secondary
            DS1
                Fields
                    dimension0
                    metric1
        Fields
            dimension0  (points to dimension0 in DS0 and DS1)
            metric0  (points to metric0 in DS0)
            metric1  (points to metric0 in DS1)
            complex_metric DS0.metric0/DS1.metric1 (Composed of fields metric0 and metric1)

    Calling this function for each of the following will yield
    DS0 -> [DS0.dimension0, DS0.metric0]
    DS1 -> [DS1.dimension0, DS1.metric1]
    Blender -> [Blender.dimension0, Blender.metric0, Blender.metric1, DS0.metric0, DS1.metric1]
        (the Blender has a reference to each field in each DS and also a complex metric composed of metric0 and metric1)
    """
    complex_fields = []
    for field in dataset.fields:
        if isinstance(field.definition, Field):
            yield field
        else:
            complex_fields.append(field)

    yield from find_dataset_fields(complex_fields)


@listify
def _map_field(dataset, fields, dimension_map=None):
    for field in fields:
        if dimension_map is not None and field.definition in dimension_map:
            yield field, dimension_map[field.definition]
        if field.definition in dataset.fields:
            yield field, field.definition
            # also yield the dataset field mapped to itself so that any reference to this field (from blender or
            # from dataset) can be used.
            yield field.definition, field.definition
        if field in dataset.fields:
            yield field, field


def _datasets_and_field_maps(blender):
    from fireant.dataset.data_blending import DataSetBlender

    def _flatten_blend_datasets(dataset) -> List:
        primary = dataset.primary_dataset
        secondary = dataset.secondary_dataset
        # TODO explain this
        dataset_fields = _find_dataset_fields_needed_to_be_mapped(dataset)

        blender2primary_field_map = dict(_map_field(primary, dataset_fields))
        blender2secondary_field_map = dict(
            _map_field(secondary, dataset_fields, dataset.dimension_map)
        )

        if not isinstance(primary, DataSetBlender):
            return [
                (primary, blender2primary_field_map),
                (secondary, blender2secondary_field_map),
            ]

        # get the dataset children of the blender (`dataset.primary_dataset`) and their corresponding field_maps,
        # then update the field map to reference this blender's field (`dataset`)
        datasets_and_field_maps = []
        for ds, fm in _flatten_blend_datasets(primary):
            remapped_field_map = {**fm}
            for field in dataset_fields:
                if (
                    field not in blender2primary_field_map
                    or blender2primary_field_map[field] not in fm
                ):
                    continue
                remapped_field_map[field] = fm[blender2primary_field_map[field]]

            datasets_and_field_maps.append((ds, remapped_field_map))

        return [
            *datasets_and_field_maps,
            (secondary, blender2secondary_field_map),
        ]

    return zip(*_flatten_blend_datasets(blender))


class EmptyWidget(Widget):

    @property
    def metrics(self):
        if 0 == len(self.items):
            return []

        return super().metrics


def _build_dataset_query(dataset, field_map, metrics, dimensions, filters, references, operations):

    @listify
    def _map_fields(fields):
        """
        TODO describe this
        """
        for field in fields:
            field_from_blender = find_field_in_modified_field(field)
            if field_from_blender in dataset.fields:
                yield field
                continue
            if field_from_blender not in field_map:
                continue

            yield field.for_(field_map[field_from_blender])

    dataset_metrics = ordered_distinct_list_by_attr(_map_fields(metrics))
    dataset_dimensions = _map_fields(dimensions)
    dataset_filters = _map_fields(filters)
    dataset_references = _map_fields(references)

    if not any([dataset_metrics, dataset_dimensions]):
        return []

    # filter out operations that are relevant for this dataset
    dataset_operations = []
    dataset_metrics_aliases = [metric.alias for metric in dataset_metrics]
    for operation in operations:
        for metric in operation.metrics:
            if metric.alias in dataset_metrics_aliases:
                dataset_operations.append(operation)

    share_dimensions = find_share_dimensions(dataset_dimensions, operations)

    return make_slicer_query_with_totals_and_references(
        dataset.database,
        dataset.table,
        dataset.joins,
        dataset_dimensions,
        dataset_metrics,
        dataset_operations,
        dataset_filters,
        dataset_references,
        [],
        share_dimensions=share_dimensions,
    )


def _blender_join_criteria(
    base_query, join_query, dimensions, base_field_map, join_field_map
):
    """
    Build a criteria for joining this join query to the base query in dataset blender queries. This should be a set of
    equality conditions like A0=B0 AND A1=B1 AND An=Bn for each mapped dimension between dataset from
    `DataSetBlender.dimension_map`.
    """
    join_criteria = None
    for dimension in dimensions:
        dimension = find_field_in_modified_field(dimension)
        if not all([dimension in base_field_map, dimension in join_field_map]):
            continue

        alias0, alias1 = [
            alias_selector(field_map[dimension].alias)
            for field_map in [base_field_map, join_field_map]
        ]

        next_criteria = base_query[alias0] == join_query[alias1]
        join_criteria = (
            next_criteria if join_criteria is None else (join_criteria & next_criteria)
        )

    return join_criteria


def _blend_query(dimensions, metrics, orders, field_maps, queries):
    for i, query in enumerate(queries):
        if query:
            queries = queries[i:]
            field_maps = field_maps[i:]
            break
    else:
        return None

    base_query = queries[0]
    base_field_map = field_maps[0]
    join_queries = queries[1:]
    join_field_maps = field_maps[1:]

    blender_query = Query.from_(base_query, immutable=False)
    for join_sql, join_field_map in zip(join_queries, join_field_maps):
        if join_sql is None:
            continue

        criteria = _blender_join_criteria(
            base_query, join_sql, dimensions, base_field_map, join_field_map
        )

        # In most cases there are dimensions to join the two data blending queries on, but if there are none, then
        # instead of doing a join, add the data blending query to the from clause
        blender_query = (
            blender_query.from_(join_sql)  # <-- no dimensions mapped
            if criteria is None
            else blender_query.join(join_sql).on(criteria)  # <-- mapped dimensions
        )

    def _get_sq_field_for_blender_field(field, reference=None):
        unmodified_field = find_field_in_modified_field(field)
        field_alias = alias_selector(reference_alias(field, reference))

        # search for the field in each field map to determine which subquery it will be in
        for query, field_map in zip(queries, field_maps):
            if query is None or unmodified_field not in field_map:
                continue

            mapped_field = field_map[unmodified_field]
            mapped_field_alias = alias_selector(
                reference_alias(mapped_field, reference)
            )

            subquery_field = query[mapped_field_alias]
            # case #1 modified fields, ex. day(timestamp) or rollup(dimension)
            return field.for_(subquery_field).as_(field_alias)

        # Need to copy the metrics if there are references so that the `get_sql` monkey patch does not conflict
        definition = copy.deepcopy(field.definition)
        # case #2: complex blender fields
        return definition.as_(field_alias)

    reference = base_query._references[0] if base_query._references else None

    # WARNING: In order to make complex fields work, the get_sql for each field is monkey patched in. This must
    # happen here because a complex metric by definition references values selected from the dataset subqueries.

    for metric in find_dataset_fields(metrics):
        subquery_field = _get_sq_field_for_blender_field(metric, reference)
        metric.get_sql = subquery_field.get_sql

    sq_dimensions = [_get_sq_field_for_blender_field(d) for d in dimensions]
    sq_metrics = [_get_sq_field_for_blender_field(m, reference) for m in metrics]
    blender_query = blender_query.select(*sq_dimensions).select(*sq_metrics)

    for field, orientation in orders:
        orderby_field = _get_sq_field_for_blender_field(field)
        blender_query = blender_query.orderby(orderby_field, order=orientation)

    return blender_query


class DataSetBlenderQueryBuilder(DataSetQueryBuilder):
    """
    Blended dataset queries consist of widgets, dimensions, filters, orders by and references. At least one or
    more widgets is required. All others are optional.
    """

    @property
    def sql(self):
        """
        Serialize this query builder to a list of Pypika/SQL queries. This function will return one query for every
        combination of reference and rolled up dimension (including null options).

        This collects all of the metrics in each widget, dimensions, and filters and builds a corresponding pypika query
        to fetch the data.  When references are used, the base query normally produced is wrapped in an outer query and
        a query for each reference is joined based on the referenced dimension shifted.

        :return: a list of Pypika's Query subclass instances.
        """
        # First run validation for the query on all widgets
        self._validate()

        datasets, field_maps = _datasets_and_field_maps(self.dataset)
        metrics = find_metrics_for_widgets(self._widgets)
        dataset_metrics = find_dataset_fields(metrics)
        operations = find_operations_for_widgets(self._widgets)
        share_operations = find_share_operations(operations)

        datasets_queries = []
        for dataset, field_map in zip(datasets, field_maps):
            datasets_queries.append(_build_dataset_query(
                dataset,
                field_map,
                dataset_metrics,
                self._dimensions,
                self._filters,
                self._references,
                share_operations
            ))

        """
        A dataset query can yield one or more sql queries, depending on how many types of references or dimensions 
        with totals are selected. A blended dataset query must yield the same number and types of sql queries, but each
        blended together. The individual dataset queries built above will always yield the same number of sql queries, 
        so here those lists of sql queries are zipped.
        
               base   ref  totals ref+totals
        ds1 | ds1_a  ds1_b  ds1_c   ds1_d  
        ds2 | ds2_a  ds2_b  ds2_c   ds2_d  
        
        More concretely, using the diagram above as a reference, a dataset query with 1 reference and 1 totals dimension
        would yield 4 sql queries. With data blending with 1 reference and 1 totals dimension, 4 sql queries must also 
        be produced.  This next line converts the list of rows of the table in the diagram to a list of columns. Each 
        set of queries in a column are then reduced to a single data blending sql query. 
        """

        # TODO: what if some of the datasets end up doing more reference/total calculations? Make sure to prevent this.
        per_dataset_queries_count = len(datasets_queries[0])
        query_sets = [[] for _ in range(per_dataset_queries_count)]

        for dataset_queries in datasets_queries:
            if len(dataset_queries) != per_dataset_queries_count:
                # This shouldn't happen but we have it here as sanity check.
                print("dataset_queries length mismatch")

            for i, dataset_query in enumerate(dataset_queries):
                if dataset_query:
                    query_sets[i].append(dataset_query)

        blended_queries = []
        for queryset in query_sets:
            if len(queryset) == 1:
                blended_queries.append(queryset[0])
            else:
                blended_query = _blend_query(self._dimensions, metrics, self.orders, field_maps, queryset)
                if blended_query:
                    blended_queries.append(blended_query)

        return blended_queries
