# SQLAlchemy Easy Soft-Delete

[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/sqlalchemy-easy-softdelete?v=4)](https://pypi.org/project/sqlalchemy-easy-softdelete/)
[![PyPI - Version](https://img.shields.io/pypi/v/sqlalchemy-easy-softdelete?v=4)](https://pypi.org/project/sqlalchemy-easy-softdelete/)
[![PyPI - Types](https://img.shields.io/pypi/types/sqlalchemy-easy-softdelete?v=4)](https://pypi.org/project/sqlalchemy-easy-softdelete/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/sqlalchemy-easy-softdelete?v=4)](https://pypi.org/project/sqlalchemy-easy-softdelete/)
![Supported SQLAlchemy Versions](https://img.shields.io/badge/SQLAlchemy-2.0%20%7C%202.1-brightgreen?v=4)

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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Integer
from datetime import datetime

# Create a Class that inherits from our class builder
class SoftDeleteMixin(
    generate_soft_delete_mixin_class(  # type: ignore[misc]
        # This table will be ignored by the hook
        # even if the table has the soft-delete column
        ignored_tables=[IgnoredTable(table_schema="public", name="cars"),]
    )
):
    # The `type: ignore[misc]` above is required because the mixin is dynamically generated

    # Type hint for IDE autocomplete and type checker support.
    # Using Mapped[T | None] ensures type checkers understand this is a
    # SQLAlchemy column that supports query operations like .where()
    deleted_at: Mapped[datetime | None]

    # Optional: Add method stubs for delete/undelete for type checker support.
    # The actual implementations are provided by the generated mixin class.
    def delete(self, v: datetime | None = None) -> None:
        super().delete(v)  # type: ignore[misc]

    def undelete(self) -> None:
        super().undelete()  # type: ignore[misc]

# Apply the mixin to your Models
class Base(DeclarativeBase):
    pass

class Fruit(Base, SoftDeleteMixin):
    __tablename__ = "fruit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
```

### Example Usage:

```py
all_active_fruits = session.query(Fruit).all()
```
This will generate a query with an automatic `WHERE fruit.deleted_at IS NULL` condition added to it.

```py
all_fruits = session.query(Fruit).execution_options(include_deleted=True).all()
```
Setting `include_deleted=True` (attribute name can be customized) in the query disables soft delete for that query. It can also be given at execution time, as in `session.execute(stmt, execution_options={"include_deleted": True})` or `session.get(Fruit, 1, execution_options={"include_deleted": True})`. It also covers the relationships the query eager loads (joined, selectin, subquery and immediate loads, whether set in options or on the relationship), but not the lazy loads that happen later.

The rewrite never modifies your statement or anything inside it, so statements, subqueries and `lambda_stmt()` objects can be built once and executed many times, with or without `include_deleted`.

### Rewrite cache

To know which tables a SELECT reads from, the library has SQLAlchemy work out the statement's final FROM clause, which for an ORM statement runs SQLAlchemy's ORM compilation of it, outside of SQLAlchemy's own compiled cache. The library remembers what it adds to a statement by the statement's cache key, so later statements of the same shape (the same SQL, with any parameter values) skip that work.

The cache holds 500 statement shapes by default, the same as SQLAlchemy's default `query_cache_size`. An application with more distinct statements can raise it, and `0` disables it:

```py
class SoftDeleteMixin(generate_soft_delete_mixin_class(rewrite_cache_size=2000)):  # type: ignore[misc]
    deleted_at: Mapped[datetime | None]
```

Each entry takes 1-4 KB, most of it the statement's cache key. To see how well the size fits, read the cache statistics of the mixin's rewriter. `misses` counts the statement shapes that were analyzed and then remembered: a `misses` count that keeps growing while `size` stays at `max_size` means the cache is too small.

```py
SoftDeleteMixin._sqlalchemy_easy_softdelete_rewriter.rewrite_cache_info()
# RewriteCacheInfo(hits=48210, misses=212, uncacheable=35, size=212, max_size=500)
```

`uncacheable` counts the statements whose rewrite cannot be remembered, and is redone on every execution, such as statements without a SQLAlchemy cache key.

### Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

### License

* BSD-3-Clause
