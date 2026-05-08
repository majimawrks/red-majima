from .warera_ask import WareraAsk


async def setup(bot):
    await bot.add_cog(WareraAsk(bot))
