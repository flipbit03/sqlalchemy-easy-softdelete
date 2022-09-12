# -*- coding: utf-8 -*-
# snapshottest: v1 - https://goo.gl/zC4yUc
from __future__ import unicode_literals

from snapshottest import GenericRepr, Snapshot


snapshots = Snapshot()

snapshots['test_ensure_aggregate_from_multiple_table_deletion_works_active_object_count 1'] = '''SELECT count(*) AS count_1 
FROM sdchild JOIN sdparent ON sdparent.id = sdchild.parent_id 
WHERE sdchild.deleted_at IS NULL AND sdparent.deleted_at IS NULL'''

snapshots['test_ensure_aggregate_from_multiple_table_deletion_works_active_object_count 2'] = 1

snapshots['test_ensure_table_with_inheritance_works 1'] = '''SELECT sdderivedrequest.id, sdbaserequest.id AS id_1, sdbaserequest.deleted_at, sdbaserequest.request_type 
FROM sdbaserequest JOIN sdderivedrequest ON sdbaserequest.id = sdderivedrequest.id 
WHERE sdbaserequest.deleted_at IS NULL'''

snapshots['test_ensure_table_with_inheritance_works 2'] = [
    GenericRepr('<SDDerivedRequest id=0>'),
    GenericRepr('<SDDerivedRequest id=1>')
]

snapshots['test_ensure_table_with_inheritance_works 3'] = [
    GenericRepr('<SDDerivedRequest id=0>'),
    GenericRepr('<SDDerivedRequest id=1>'),
    GenericRepr('<SDDerivedRequest id=2>')
]

snapshots['test_query_single_table 1'] = '''SELECT sdchild.id, sdchild.deleted_at, sdchild.parent_id 
FROM sdchild 
WHERE sdchild.deleted_at IS NULL'''

snapshots['test_query_single_table 2'] = [
    GenericRepr('<SDChild id=0 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1000 deleted=False    (parent_id=1)>'),
    GenericRepr('<SDChild id=2000 deleted=False    (parent_id=2)>')
]

snapshots['test_query_union_sdchild 1'] = '''SELECT anon_1.sdchild_id, anon_1.sdchild_deleted_at, anon_1.sdchild_parent_id 
FROM (SELECT sdchild.id AS sdchild_id, sdchild.deleted_at AS sdchild_deleted_at, sdchild.parent_id AS sdchild_parent_id 
FROM sdchild 
WHERE sdchild.deleted_at IS NULL UNION SELECT sdchild.id AS sdchild_id, sdchild.deleted_at AS sdchild_deleted_at, sdchild.parent_id AS sdchild_parent_id 
FROM sdchild 
WHERE sdchild.deleted_at IS NULL) AS anon_1'''

snapshots['test_query_union_sdchild 2'] = [
    GenericRepr('<SDChild id=0 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1000 deleted=False    (parent_id=1)>'),
    GenericRepr('<SDChild id=2000 deleted=False    (parent_id=2)>')
]

snapshots['test_query_with_join 1'] = '''SELECT sdchild.id, sdchild.deleted_at, sdchild.parent_id 
FROM sdchild JOIN sdparent ON sdparent.id = sdchild.parent_id 
WHERE sdchild.deleted_at IS NULL AND sdparent.deleted_at IS NULL'''

snapshots['test_query_with_join 2'] = [
    GenericRepr('<SDChild id=0 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1000 deleted=False    (parent_id=1)>')
]

snapshots['test_query_with_table_clause_as_table 1'] = '''SELECT id 
FROM sdderivedrequest'''

snapshots['test_query_with_text_clause_as_table 1'] = '''SELECT id 
FROM sdderivedrequest'''

snapshots['test_query_with_union_but_union_softdelete_disabled 1'] = '''SELECT anon_1.sdchild_id, anon_1.sdchild_deleted_at, anon_1.sdchild_parent_id 
FROM (SELECT sdchild.id AS sdchild_id, sdchild.deleted_at AS sdchild_deleted_at, sdchild.parent_id AS sdchild_parent_id 
FROM sdchild 
WHERE sdchild.deleted_at IS NULL UNION SELECT sdchild.id AS sdchild_id, sdchild.deleted_at AS sdchild_deleted_at, sdchild.parent_id AS sdchild_parent_id 
FROM sdchild) AS anon_1'''

snapshots['test_query_with_union_but_union_softdelete_disabled 2'] = [
    GenericRepr('<SDChild id=0 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=1 deleted=False       (parent_id=0)>'),
    GenericRepr('<SDChild id=2 deleted=True        (parent_id=0)>'),
    GenericRepr('<SDChild id=3 deleted=True        (parent_id=0)>'),
    GenericRepr('<SDChild id=1000 deleted=False    (parent_id=1)>'),
    GenericRepr('<SDChild id=1001 deleted=True     (parent_id=1)>'),
    GenericRepr('<SDChild id=1002 deleted=True     (parent_id=1)>'),
    GenericRepr('<SDChild id=1003 deleted=True     (parent_id=1)>'),
    GenericRepr('<SDChild id=2000 deleted=False    (parent_id=2)>'),
    GenericRepr('<SDChild id=2001 deleted=True     (parent_id=2)>')
]
