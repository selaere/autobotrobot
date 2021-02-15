import subprocess
import asyncio
import argparse
import random
from numpy.random import default_rng
import re
import discord.ext.commands

import tio
import util

def setup(bot):
    cleaner = discord.ext.commands.clean_content()
    def clean(ctx, text):
        return cleaner.convert(ctx, text)

    @bot.command(help="Gives you a random fortune as generated by `fortune`.")
    async def fortune(ctx):
        await ctx.send(subprocess.run(["fortune"], stdout=subprocess.PIPE, encoding="UTF-8").stdout)

    @bot.command(help="Generates an apioform type.")
    async def apioform(ctx):
        await ctx.send(util.apioform())

    @bot.command(help="Says Pong.")
    async def ping(ctx):
        await ctx.send("Pong.")

    @bot.command(help="Deletes the specified target.", rest_is_raw=True)
    async def delete(ctx, *, raw_target):
        target = await clean(ctx, raw_target.strip().replace("\n", " "))
        if len(target) > 256:
            await ctx.send(embed=util.error_embed("Deletion target must be max 256 chars"))
            return
        async with ctx.typing():
            await ctx.send(f"Deleting {target}...")
            await asyncio.sleep(1)
            await bot.database.execute("INSERT INTO deleted_items (timestamp, item) VALUES (?, ?)", (util.timestamp(), target))
            await bot.database.commit()
            await ctx.send(f"Deleted {target} successfully.")

    @bot.command(help="View recently deleted things, optionally matching a filter.")
    async def list_deleted(ctx, search=None):
        acc = "Recently deleted:\n"
        if search: acc = f"Recently deleted (matching {search}):\n"
        csr = None
        if search:
            csr = bot.database.execute("SELECT * FROM deleted_items WHERE item LIKE ? ORDER BY timestamp DESC LIMIT 100", (f"%{search}%",))
        else:
            csr = bot.database.execute("SELECT * FROM deleted_items ORDER BY timestamp DESC LIMIT 100")
        async with csr as cursor:
            async for row in cursor:
                to_add = "- " + row[2].replace("```", "[REDACTED]") + "\n"
                if len(acc + to_add) > 2000:
                    break
                acc += to_add
        await ctx.send(acc)

    # Python, for some *very intelligent reason*, makes the default ArgumentParser exit the program on error.
    # This is obviously undesirable behavior in a Discord bot, so we override this.
    class NonExitingArgumentParser(argparse.ArgumentParser):
        def exit(self, status=0, message=None):
            if status:
                raise Exception(f'Flag parse error: {message}')
            exit(status)

    EXEC_REGEX = "^(.*)```([a-zA-Z0-9_\\-+]+)?\n(.*)```$"

    exec_flag_parser = NonExitingArgumentParser(add_help=False)
    exec_flag_parser.add_argument("--verbose", "-v", action="store_true")
    exec_flag_parser.add_argument("--language", "-L")

    @bot.command(rest_is_raw=True, help="Execute provided code (in a codeblock) using TIO.run.")
    async def exec(ctx, *, arg):
        match = re.match(EXEC_REGEX, arg, flags=re.DOTALL)
        if match == None:
            await ctx.send(embed=util.error_embed("Invalid format. Expected a codeblock."))
            return
        flags_raw = match.group(1)
        flags = exec_flag_parser.parse_args(flags_raw.split())
        lang = flags.language or match.group(2)
        if not lang:
            await ctx.send(embed=util.error_embed("No language specified. Use the -L flag or add a language to your codeblock."))
            return
        lang = lang.strip()
        code = match.group(3)

        async with ctx.typing():
            ok, real_lang, result, debug = await tio.run(lang, code)
            if not ok:
                await ctx.send(embed=util.error_embed(util.gen_codeblock(result), "Execution failed"))
            else:
                out = result
                if flags.verbose: 
                    debug_block = "\n" + util.gen_codeblock(f"""{debug}\nLanguage:  {real_lang}""")
                    out = out[:2000 - len(debug_block)] + debug_block
                else:
                    out = out[:2000]
                await ctx.send(out)

    @bot.command(help="List supported languages, optionally matching a filter.")
    async def supported_langs(ctx, search=None):
        langs = sorted(tio.languages())
        acc = ""
        for lang in langs:
            if len(acc + lang) > 2000:
                await ctx.send(acc)
                acc = ""
            if search == None or search in lang: acc += lang + " "
        if acc == "": acc = "No results."
        await ctx.send(acc)

    @bot.command(help="Get some information about the bot.", aliases=["invite"])
    async def about(ctx):
        await ctx.send("""**AutoBotRobot: The least useful Discord bot ever designed.**
    AutoBotRobot has many features, but not necessarily any practical ones.
    It can execute code via TIO.run, do reminders, print fortunes, and not any more!
    AutoBotRobot is open source - the code is available at <https://github.com/osmarks/autobotrobot> - and you could run your own instance if you wanted to and could get around the complete lack of user guide or documentation.
    You can also invite it to your server: <https://discordapp.com/oauth2/authorize?&client_id=509849474647064576&scope=bot&permissions=68608>
    """)

    @bot.command(help="Roll simulated dice (basic NdX syntax, N <= 50, X <= 1e6).")
    async def roll(ctx, dice):
        match = re.match("([-0-9]*)d([0-9]+)", dice)
        if not match: raise ValueError("Invalid dice notation")
        n, x = match.groups()
        if n == "": n = 1
        n, x = int(n), int(x)
        if n > 50 or x > 1e6: raise ValueError("N or X exceeds limit")
        rolls = [ random.randint(1, x) for _ in range(n) ]
        await ctx.send(f"{sum(rolls)} ({' '.join(map(str, sorted(rolls)))})")

    def weight(thing):
        lthing = thing.lower()
        weight = 1.0
        if lthing == "c": weight *= 0.3
        for bad_thing in util.config["autobias"]["bad_things"]:
            if bad_thing in lthing: weight *= 0.5
        for good_thing in util.config["autobias"]["good_things"]:
            if good_thing in lthing: weight *= 2.0
        for negation in util.config["autobias"]["negations"]:
            for _ in range(lthing.count(negation)): weight = 1 / weight
        return weight

    rng = default_rng()

    @bot.command(help="'Randomly' choose between the specified options.", name="choice", aliases=["choose"])
    async def random_choice(ctx, *choices):
        choices = list(choices)
        samples = 1
        # apparently doing typing.Optional[int] doesn't work properly with this, so just bodge around it
        try:
            samples = int(choices[0])
            choices.pop(0)
        except: pass

        if samples > 9223372036854775807 or samples < 1 or len(choices) < 1:
            await ctx.send("No.")
            return

        # because of python weirdness, using sum() on the bare map iterator consumes it, which means we have to actually make a list
        weights = list(map(weight, choices))

        if samples == 1: return await ctx.send(random.choices(choices, weights=weights, k=1)[0])

        total = sum(weights)
        probabilities = list(map(lambda x: x / total, weights))
        results = map(lambda t: (choices[t[0]], t[1]), enumerate(rng.multinomial(samples, list(probabilities))))

        await ctx.send("\n".join(map(lambda x: f"{x[0]} x{x[1]}", results)))