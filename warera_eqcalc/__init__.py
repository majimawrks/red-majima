from .eqcalc import EqCalc


async def setup(bot):
    await bot.add_cog(EqCalc(bot))
