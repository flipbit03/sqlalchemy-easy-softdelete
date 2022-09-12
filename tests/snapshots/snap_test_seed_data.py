# -*- coding: utf-8 -*-
# snapshottest: v1 - https://goo.gl/zC4yUc
from __future__ import unicode_literals

from snapshottest import GenericRepr, Snapshot


snapshots = Snapshot()

snapshots['test_ensure_stable_seed_data 1'] = [
    GenericRepr('<SDParent id=0 deleted=False>'),
    GenericRepr('<SDParent id=1 deleted=False>'),
    GenericRepr('<SDParent id=2 deleted=True>')
]

snapshots['test_ensure_stable_seed_data 2'] = [
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
