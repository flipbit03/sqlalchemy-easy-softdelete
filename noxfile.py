import nox


@nox.session
def tests(session):
    session.install('pytest')
    session.run('pytest')
