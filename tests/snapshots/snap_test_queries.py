# -*- coding: utf-8 -*-
# snapshottest: v1 - https://goo.gl/zC4yUc
from __future__ import unicode_literals

from snapshottest import GenericRepr, Snapshot


snapshots = Snapshot()

snapshots['test_ensure_aggregate_from_multiple_table_deletion_works_active_object_count 1'] = 1

snapshots['test_ensure_table_with_inheritance_works 1'] = [
    GenericRepr('<SDDerivedRequest id=1000>'),
    GenericRepr('<SDDerivedRequest id=1001>')
]

snapshots['test_ensure_table_with_inheritance_works 2'] = [
    GenericRepr('<SDDerivedRequest id=1000>'),
    GenericRepr('<SDDerivedRequest id=1001>'),
    GenericRepr('<SDDerivedRequest id=1002>')
]

snapshots['test_query_single_table 1'] = [
    GenericRepr('<SDChild id=100000 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100001 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100002 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100003 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100100 deleted=False (parent_id=1001)>'),
    GenericRepr('<SDChild id=100200 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100201 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100202 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100203 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100204 deleted=False (parent_id=1002)>')
]

snapshots['test_query_union_sdchild 1'] = [
    GenericRepr('<SDChild id=100000 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100001 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100002 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100003 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100100 deleted=False (parent_id=1001)>'),
    GenericRepr('<SDChild id=100200 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100201 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100202 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100203 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100204 deleted=False (parent_id=1002)>')
]

snapshots['test_query_with_join 1'] = [
    GenericRepr('<SDChild id=100000 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100001 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100002 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100003 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100100 deleted=False (parent_id=1001)>')
]

snapshots['test_query_with_table_clause_as_table 1'] = '''SELECT id 
FROM sdderivedrequest'''

snapshots['test_query_with_text_clause_as_table 1'] = '''SELECT id 
FROM sdderivedrequest'''

snapshots['test_query_with_union_but_union_softdelete_disabled 1'] = [
    GenericRepr('<SDChild id=100000 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100001 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100002 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100003 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=100004 deleted=True (parent_id=1000)>'),
    GenericRepr('<SDChild id=100100 deleted=False (parent_id=1001)>'),
    GenericRepr('<SDChild id=100101 deleted=True (parent_id=1001)>'),
    GenericRepr('<SDChild id=100102 deleted=True (parent_id=1001)>'),
    GenericRepr('<SDChild id=100200 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100201 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100202 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100203 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100204 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=100205 deleted=True (parent_id=1002)>'),
    GenericRepr('<SDChild id=100206 deleted=True (parent_id=1002)>'),
    GenericRepr('<SDChild id=100207 deleted=True (parent_id=1002)>'),
    GenericRepr('<SDChild id=100208 deleted=True (parent_id=1002)>')
]
