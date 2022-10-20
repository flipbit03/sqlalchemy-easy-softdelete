# -*- coding: utf-8 -*-
# snapshottest: v1 - https://goo.gl/zC4yUc
from __future__ import unicode_literals

from snapshottest import GenericRepr, Snapshot

snapshots = Snapshot()

snapshots['test_ensure_stable_seed_data 1'] = [
    GenericRepr('<SDParent id=1000 deleted=False>'),
    GenericRepr('<SDParent id=1001 deleted=False>'),
    GenericRepr('<SDParent id=1002 deleted=True>')
]

snapshots['test_ensure_stable_seed_data 2'] = [
    GenericRepr('<SDChild id=1000000 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=1000001 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=1000002 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=1000003 deleted=False (parent_id=1000)>'),
    GenericRepr('<SDChild id=1000004 deleted=True (parent_id=1000)>'),
    GenericRepr('<SDChild id=1001000 deleted=False (parent_id=1001)>'),
    GenericRepr('<SDChild id=1001001 deleted=True (parent_id=1001)>'),
    GenericRepr('<SDChild id=1001002 deleted=True (parent_id=1001)>'),
    GenericRepr('<SDChild id=1002000 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=1002001 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=1002002 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=1002003 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=1002004 deleted=False (parent_id=1002)>'),
    GenericRepr('<SDChild id=1002005 deleted=True (parent_id=1002)>'),
    GenericRepr('<SDChild id=1002006 deleted=True (parent_id=1002)>'),
    GenericRepr('<SDChild id=1002007 deleted=True (parent_id=1002)>'),
    GenericRepr('<SDChild id=1002008 deleted=True (parent_id=1002)>'),
    GenericRepr('<SDChild id=1002009 deleted=True (parent_id=1002)>')
]
