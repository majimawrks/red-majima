from .alliance_mcu import AllianceMCU


async def setup(bot):
    await bot.add_cog(AllianceMCU(bot))
