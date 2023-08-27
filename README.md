# SQLAlchemy Easy Soft-Delete

[![pypi](https://img.shields.io/pypi/v/sqlalchemy-easy-softdelete.svg)](https://pypi.org/project/sqlalchemy-easy-softdelete/)
[![python](https://img.shields.io/pypi/pyversions/sqlalchemy-easy-softdelete.svg)](https://pypi.org/project/sqlalchemy-easy-softdelete/)
[![Build Status](https://github.com/flipbit03/sqlalchemy-easy-softdelete/actions/workflows/test_matrix.yml/badge.svg)](https://github.com/flipbit03/sqlalchemy-easy-softdelete/actions/workflows/test.yml)
![Supported SQLAlchemy Versions](https://img.shields.io/badge/SQLAlchemy-1.4%20%2F%202.0-brightgreen)

[//]: # ([![codecov]&#40;https://codecov.io/gh/flipbit03/sqlalchemy-easy-softdelete/branch/main/graphs/badge.svg&#41;]&#40;https://codecov.io/github/flipbit03/sqlalchemy-easy-softdelete&#41;)

Easily add soft-deletion to your SQLAlchemy Models and automatically filter out soft-deleted objects from your queries and relationships.

This package can generate a tailor-made SQLAlchemy Mixin that can be added to your SQLAlchemy Models, making them contain a field that, when set, will mark the entity as being soft-deleted.

The library also installs a hook which dynamically rewrites all selects which are sent to the database for all tables that implement the soft-delete mixin, providing a seamless experience in both manual queries and model relationship accesses.

Mixin generation is fully customizable and you can choose the field name, its type, and the presence of (soft-)delete/undelete methods.

The default implementation will generate a `deleted_at` field in your models, of type `DateTime(timezone=True)`, and will also provide a `.delete(v: Optional = datetime.utcnow())` and `.undelete()` methods.

### Installation:

```
pip install sqlalchemy-easy-softdelete
```

### How to use:

```py
from sqlalchemy_easy_softdelete.mixin import generate_soft_delete_mixin_class
from sqlalchemy_easy_softdelete.hook import IgnoredTable
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer
from datetime import datetime

# Create a Class that inherits from our class builder
class SoftDeleteMixin(generate_soft_delete_mixin_class(
    # This table will be ignored by the hook
    # even if the table has the soft-delete column
    ignored_tables=[IgnoredTable(table_schema="public", name="cars"),]
)):
    # type hint for autocomplete IDE support
    deleted_at: datetime

# Apply the mixin to your Models
Base = declarative_base()

class Fruit(Base, SoftDeleteMixin):
    __tablename__ = "fruit"
    id = Column(Integer)
```

### Example Usage:

```py
all_active_fruits = session.query(Fruit).all()
```
This will generate a query with an automatic `WHERE fruit.deleted_at IS NULL` condition added to it.

```py
all_fruits = session.query(Fruit).execution_options(include_deleted=True).all()
```
Setting `include_deleted=True` (attribute name can be customized) in the query disables soft delete for that query.

#### License

* BSD-3-Clause

[//]: # (* Documentation: <https://flipbit03.github.io/sqlalchemy-easy-softdelete>)
[//]: # (* GitHub: <https://github.com/flipbit03/sqlalchemy-easy-softdelete>)
[//]: # (* PyPI: <https://pypi.org/project/sqlalchemy-easy-softdelete/>)
