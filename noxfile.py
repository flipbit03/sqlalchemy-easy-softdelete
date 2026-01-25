import nox

# SQLAlchemy 2.0.35+ is required for Python 3.13+
# Older versions fail with symbol errors on 3.13+
SQLA_VERSIONS = {
    "3.10": ["2.0.46", "2.0.30", "2.0.10", "1.4.54"],
    "3.11": ["2.0.46", "2.0.30", "2.0.10", "1.4.54"],
    "3.12": ["2.0.46", "2.0.30", "2.0.10", "1.4.54"],
    "3.13": ["2.0.46"],  # Only 2.0.35+ supports Python 3.13
    "3.14": ["2.0.46"],  # Only 2.0.35+ supports Python 3.14
}


@nox.session(python=list(SQLA_VERSIONS.keys()))
def tests(session):
    """Run tests with SQLAlchemy versions compatible with the Python version."""
    python_version = f"{session.python.split('.')[0]}.{session.python.split('.')[1]}"

    sqla_versions = SQLA_VERSIONS.get(python_version, ["2.0.46"])

    for sqla_version in sqla_versions:
        session.install(f"SQLAlchemy=={sqla_version}")
        session.install("pytest")
        session.install("syrupy")
        session.install("psycopg2-binary")
        session.run("pytest", "-v", "--tb=short")
