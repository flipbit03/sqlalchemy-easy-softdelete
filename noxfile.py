import nox

# SQLAlchemy 2.0.35+ is required for Python 3.13+
# Older versions fail with symbol errors on 3.13+
# SQLAlchemy 2.1 requires Python 3.11+
SQLA_VERSIONS = {
    "3.10": ["2.0.56", "2.0.30", "2.0.20"],
    "3.11": ["2.1.7", "2.1.0", "2.0.56", "2.0.30", "2.0.20"],
    "3.12": ["2.1.7", "2.1.0", "2.0.56", "2.0.30", "2.0.20"],
    "3.13": ["2.1.7", "2.1.0", "2.0.56"],  # Only 2.0.35+ supports Python 3.13
    "3.14": ["2.1.7", "2.1.0", "2.0.56"],  # Only 2.0.35+ supports Python 3.14
    "3.15": ["2.1.7", "2.1.0", "2.0.56"],  # SQLAlchemy declares Python 3.15 support from 2.0.52
}


@nox.session(python=list(SQLA_VERSIONS.keys()))
def tests(session):
    """Run tests with SQLAlchemy versions compatible with the Python version."""
    python_version = f"{session.python.split('.')[0]}.{session.python.split('.')[1]}"

    sqla_versions = SQLA_VERSIONS.get(python_version, ["2.1.7"])

    # Pin versions to match pyproject.toml for reproducibility
    session.install("pytest>=8.3.4")
    session.install("syrupy>=4.8.0")
    session.install("psycopg2-binary>=2.9.0")

    for sqla_version in sqla_versions:
        session.install(f"SQLAlchemy=={sqla_version}")
        session.run("pytest", "-v", "--tb=short")
