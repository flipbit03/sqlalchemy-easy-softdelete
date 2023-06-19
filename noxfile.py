import nox


@nox.session(python=("3.9", "3.10", "3.11"))
@nox.parametrize("sqla_version", ["2.0.16", "1.4.48"])
def tests(session, sqla_version):
    session.install(f'SQLAlchemy=={sqla_version}')
    session.install('pytest')
    session.install('snapshottest')
    session.run('pytest')
