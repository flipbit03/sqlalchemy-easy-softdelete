import nox


@nox.session(python=("3.10", "3.11", "3.12", "3.13", "3.14"))
@nox.parametrize(
    "sqla_version",
    [
        "2.0.46",  # Latest 2.0.x
        "2.0.30",  # Mid 2.0.x
        "2.0.10",  # Early 2.0.x (2.0.0 lacks list[X] annotation support)
        "1.4.54",  # Latest 1.4.x (legacy support)
    ],
)
def tests(session, sqla_version):
    session.install(f"SQLAlchemy=={sqla_version}")
    session.install("pytest")
    session.install("syrupy")
    session.run("pytest")
