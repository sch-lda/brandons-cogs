from .follower import Follower

__red_end_user_data_statement__ = (
    "This cog does stores user's followers, who they are following, and opt in/out status."
)


async def setup(bot):
    await bot.add_cog(Follower(bot))
