import os
from .roleplay import RolePlay
from redbot.core import Config

__red_end_user_data_statement__ = "No data is stored."


async def setup(bot):
    await bot.add_cog(RolePlay(bot))
