import click

from ofd import __version__
from ofd.cli.commits import commits
from ofd.cli.digest import digest
from ofd.cli.init_cmd import init
from ofd.cli.ledger_cmd import ledger
from ofd.cli.list_cmd import list_cmd
from ofd.cli.mirror import mirror
from ofd.cli.query import query
from ofd.cli.reindex import reindex
from ofd.cli.rollouts import rollouts
from ofd.cli.run import run
from ofd.cli.show import show
from ofd.cli.watchlist_cmd import watchlist_cli


@click.group()
@click.version_option(__version__, prog_name="ofd")
def cli():
    """Odoo Framework Digest."""


cli.add_command(commits)
cli.add_command(digest)
cli.add_command(init)
cli.add_command(ledger)
cli.add_command(list_cmd, name="list")
cli.add_command(mirror)
cli.add_command(query)
cli.add_command(reindex)
cli.add_command(rollouts)
cli.add_command(run)
cli.add_command(show)
cli.add_command(watchlist_cli, name="watchlist")
