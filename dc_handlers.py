# Re-exports all DC handlers from submodules
# Handler registration happens via @app_ctx.dc_cli.on() decorators at import time

from handlers.dc.commands import *
from handlers.dc.messaging import *
from handlers.dc.events import *
