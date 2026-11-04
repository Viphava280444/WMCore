# pylint: disable=invalid-name
# the module name MUST be lowercase "sitecustomize": python's site machinery
# imports it by exactly that name at interpreter start
"""CI-only shim for the GitHub-hosted workflow (wmcore-hosted-ci.yml).

Python's site machinery imports this module at interpreter start because the
workflow puts this directory first on PYTHONPATH. mysqlclient (the MySQLdb
package) has no manylinux wheels, so SQLAlchemy's default mysql:// dialect
cannot import MySQLdb on a plain ubuntu runner; PyMySQL registers itself under
that name instead. This keeps the DATABASE URL in the same bare mysql:// shape
that env_unittest.sh uses and leaves WMQuality/TestInit.py unpatched.
"""
try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ImportError:
    pass
