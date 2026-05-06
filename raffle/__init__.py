from .raffle import Raffle

async def setup(bot):
    await bot.add_cog(Raffle(bot))
