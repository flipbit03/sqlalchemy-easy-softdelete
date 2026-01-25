# Contributing to SQLAlchemy Easy Soft-Delete

Thank you for your interest in contributing!

## Development Setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/) - Python package manager
- [Docker](https://www.docker.com/) - For running PostgreSQL

### Getting Started

1. Clone the repository:
   ```bash
   git clone https://github.com/flipbit03/sqlalchemy-easy-softdelete.git
   cd sqlalchemy-easy-softdelete
   ```

2. Install dependencies:
   ```bash
   make dev
   ```

3. Install all Python versions for testing (optional, for full test matrix):
   ```bash
   make python-versions
   ```

## Running Tests

### Quick test with SQLite (no Docker needed)

```bash
make test
```

### Test with PostgreSQL

```bash
make pg        # Start PostgreSQL
make test-pg   # Run tests against PostgreSQL
make pg-stop   # Stop PostgreSQL when done
```

### Full CI test matrix (all Python versions × SQLAlchemy versions)

```bash
make test-all-pg
```

### Test a specific Python version

```bash
make test-py version=3.12
```

## Code Quality

### Linting

```bash
make lint
```

This runs `pre-commit` with ruff for linting and formatting.

### Type Checking

```bash
make typecheck
```

## Pull Request Process

1. Ensure all tests pass: `make test-all-pg`
2. Ensure linting passes: `make lint`
3. Ensure type checking passes: `make typecheck`
4. Update documentation if needed
5. Submit your PR against the `main` branch

## Makefile Reference

| Command | Description |
|---------|-------------|
| `make dev` | Install development dependencies |
| `make lint` | Run linting (pre-commit + ruff) |
| `make typecheck` | Run type checking (mypy) |
| `make test` | Quick test with SQLite |
| `make test-pg` | Test with PostgreSQL |
| `make test-all-pg` | Full CI test matrix (with PostgreSQL) |
| `make test-py version=X.Y` | Test specific Python version |
| `make pg` | Start PostgreSQL |
| `make pg-stop` | Stop PostgreSQL |
| `make python-versions` | Install all Python versions |
| `make clean` | Clean build artifacts |
