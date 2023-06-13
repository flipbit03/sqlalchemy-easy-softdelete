import nox


@nox.session(python=("3.9", "3.10", "3.11"))
def sqla2_tests(session):
    session.install('SQLAlchemy==2.0.16')
    session.install('pytest')
    session.install('snapshottest')
    session.run('pytest')


@nox.session(python=("3.9", "3.10", "3.11"))
def sqla14_tests(session):
    session.install('SQLAlchemy==1.4.48')
    session.install('pytest')
    session.install('snapshottest')
    session.run('pytest')
