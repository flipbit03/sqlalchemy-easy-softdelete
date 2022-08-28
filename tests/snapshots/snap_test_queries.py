# -*- coding: utf-8 -*-
# snapshottest: v1 - https://goo.gl/zC4yUc
from __future__ import unicode_literals

from snapshottest import GenericRepr, Snapshot

snapshots = Snapshot()

snapshots['test_ensure_aggregate_from_multiple_table_deletion_works_active_object_count 1'] = 3

snapshots['test_ensure_multiple_table_deletion_works 1'] = [
    GenericRepr('<SDChild id=0 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1000 deleted=False    (parent_id=1)>'),
]

snapshots['test_query_single_table 1'] = [
    GenericRepr('<SDChild id=0 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1000 deleted=False    (parent_id=1)>'),
    GenericRepr('<SDChild id=2000 deleted=False    (parent_id=2)>'),
]
