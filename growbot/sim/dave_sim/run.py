"""One simulated run: the real creature page in headless Chromium, on the sim body.

    sim = Sim(Options(...))
    await sim.open()            # page up, legs attached, motion on
    await sim.say("Dave, come here")
    ok = await sim.until(lambda s: s.state["x"] < -0.1, 60, "walked toward me")
    await sim.close()           # writes the report

Every control tick (40 ms, the runner's rate) the sim body steps, the chip plays whatever
the page sent, and the phone's motion events go to the page; the front camera's pictures go
on their own, as fast as the software renderer manages (up to `fps`). A run keeps a
timeline of what Dave heard, said, sent his legs and actually did, and the checks a
scenario made; `report.py` draws it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dave_sim.box import ORIGIN, ROBOT, FakeBox, Model
from dave_sim.chip import Chip

HERE = Path(__file__).resolve().parent
CONTROL_DT = 0.04
UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Mobile Safari/537.36"
)


def _growbot_train() -> Any:
    """GrowBot_Training's sim body, from its checkout ($GROWBOT_TRAINING, else a sibling)."""
    root = Path(os.environ.get("GROWBOT_TRAINING", HERE.parents[3] / "GrowBot_Training"))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from growbot_train import simbody

    return simbody


ROBOT_WIRING: dict[str, Any] = {"swap": True, "l_sign": 1, "r_sign": -1}


@dataclass
class Options:
    model: Model
    soul: dict[str, Any] | None = None
    prefs: dict[str, str] = field(default_factory=dict)
    start: str = "lying"
    # simbody.Wiring fields. The default is the owner's robot, not the sim's stock build: its
    # trained walks only turn the right way with the page's leg swap (body test #40). Its legs
    # are mirrored, not crossed: crossing the pins alone (swap with the stock signs) also
    # negates both swings, which sent the trained forward away from the person.
    wiring: dict[str, Any] = field(default_factory=lambda: dict(ROBOT_WIRING))
    # simbody.Person fields; None: nobody in the room.
    person: dict[str, Any] | None = field(default_factory=lambda: {"x": -1.0, "face_z": 0.6})
    # The calibration the PWA saved for the robot; None: the stock one.
    cal: dict[str, Any] | None = None
    policies: dict[str, Path] = field(default_factory=dict)
    skills: dict[str, str] | None = None
    fps: float = 15.0  # the camera's cap; software rendering may manage less
    out: Path = Path(__file__).resolve().parents[1] / "sim-runs" / "run"
    query: str = ""  # extra ?flags for the page, e.g. "getupnamed=1"
    eyes: bool = True  # open the camera at the start, as the owner does
    # The follower's camera flip, as a phone whose owner set it from the Body tab's dry run.
    # In the sim, on the robot's wiring as modelled, every unflipped follow turned away from
    # the face (2026-11-17); a body test is to confirm the robot needs it too. None leaves
    # the page's default (unflipped).
    cam_flip: bool | None = True


class Sim:
    def __init__(self, opts: Options) -> None:
        self.o = opts
        sb = _growbot_train()
        self.sb = sb
        self.body = sb.SimBody(wiring=sb.Wiring(**opts.wiring))
        self.body.reset(opts.start)
        self.body.place_person(
            sb.Person(**opts.person) if opts.person else sb.Person(present=False)
        )
        self.chip = Chip(self.body)
        self.box = FakeBox(
            model=opts.model,
            soul=opts.soul,
            prefs=dict(opts.prefs),
            policies=opts.policies,
            skills=opts.skills,
        )
        if opts.cal is not None:
            self.box.cal = opts.cal
        self.t0 = time.monotonic()
        self.epoch0 = time.time()
        self.timeline: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self.frames: list[tuple[float, bytes]] = []  # (t, jpeg) of what the camera showed
        self._agent_ws: Any = None
        self._acks: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._loop: asyncio.Task[None] | None = None
        self._cam: asyncio.Task[None] | None = None
        self._drained = 0.0
        self._pw: Any = None
        self.page: Any = None

    # ------------------------------------------------------------------ the record

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def note(self, kind: str, **data: Any) -> None:
        self.timeline.append({"t": self.now(), "kind": kind, **data})

    @property
    def state(self) -> dict[str, Any]:
        return self.body.state()

    def check(self, ok: bool, what: str, **detail: Any) -> bool:
        self.checks.append({"t": self.now(), "ok": bool(ok), "what": what, **detail})
        self.note("check", ok=bool(ok), what=what)
        return bool(ok)

    # ------------------------------------------------------------------ the browser

    async def open(self) -> None:
        from playwright.async_api import async_playwright

        self.o.out.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        browser = await self._pw.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required", "--use-gl=swiftshader"]
        )
        ctx = await browser.new_context(
            viewport={"width": 412, "height": 915},
            user_agent=UA,
            is_mobile=True,
            has_touch=True,
            permissions=["camera", "microphone"],
        )
        await ctx.add_init_script(path=str(HERE / "phone.js"))
        if self.o.cam_flip is not None:
            cam = json.dumps({"flipX": self.o.cam_flip})
            await ctx.add_init_script(
                "try { if (!localStorage.getItem('gb.follow.camera'))"
                f" localStorage.setItem('gb.follow.camera', {json.dumps(cam)}); }} catch (e) {{}}"
            )
        await ctx.route(f"{ORIGIN}/**", self._http)
        await ctx.route_web_socket(f"wss://{ORIGIN.split('//')[1]}/d/**", self._ws)
        self.page = page = await ctx.new_page()
        page.on("pageerror", lambda e: self.note("page_error", error=str(e)[:300]))
        page.on(
            "console",
            lambda m: (
                self.note("console", level=m.type, text=m.text[:300])
                if m.type in ("error", "warning")
                else None
            ),
        )
        q = f"id={ROBOT}&mode=legs" + (f"&{self.o.query}" if self.o.query else "")
        await page.goto(f"{ORIGIN}/v3/next.html?{q}", wait_until="load")
        self._loop = asyncio.create_task(self._run())
        self._cam = asyncio.create_task(self._camera())
        await asyncio.sleep(2.0)
        # A creature that has a soul opens on its wake screen; the owner taps Wake.
        gate = page.locator("#wakeGate")
        if await gate.count() and await gate.is_visible():
            wake = gate.get_by_role("button", name="wake", exact=False)
            await (wake.first if await wake.count() else gate).click()
            await asyncio.sleep(1.0)
            self.note("woke")
        # A person's first tap: on Android it is what turns the motion sensors on.
        await page.mouse.click(206, 300)
        await asyncio.sleep(0.5)
        if self.o.eyes:
            await self.open_eyes()
        self.note("opened")

    async def open_eyes(self) -> None:
        """Tap the camera button, as the owner does, unless the eyes are already open."""
        if not await self.page.evaluate("() => typeof camOn !== 'undefined' && !!camOn"):
            await self.page.click("#gbkSee")
            await asyncio.sleep(1.0)
        on = await self.page.evaluate("() => typeof camOn !== 'undefined' && !!camOn")
        self.note("eyes", open=on)

    async def _http(self, route: Any) -> None:
        req = route.request
        asked = self.now()
        got = await self.box.handle(req.method, req.url[len(ORIGIN) :], req.post_data_buffer)
        await route.fulfill(status=got.status, body=got.body, headers={"content-type": got.ctype})
        # Logged only once the page has it: a reply to a request the page gave up on is
        # nothing Dave said.
        call = got.call
        if call is not None and "reply" in call and int(call["request"].get("max_tokens") or 9) > 2:
            warm = any(k.lower() == "x-gb-warm" for k in (req.headers or {}))
            self.note("dave", text=call["reply"], asked=asked, warm=warm)

    async def _ws(self, ws: Any) -> None:
        agent = ws.url.endswith("-agent")
        if agent:
            self._agent_ws = ws
            ws.on_message(lambda m: self._agent_frame(str(m)))
            return

        def on(m: Any) -> None:
            for reply in self.chip.frame(str(m)):
                ws.send(json.dumps(reply))
            f = self.chip.log[-1] if self.chip.log else {}
            if f.get("t") in ("act", "routine", "stop"):
                self.note("legs", frame=f)

        ws.on_message(on)

    def _agent_frame(self, text: str) -> None:
        try:
            m = json.loads(text)
        except ValueError:
            return
        fut = self._acks.pop(m.get("rid"), None) if m.get("t") == "box_ack" else None
        if fut is not None and not fut.done():
            fut.set_result(m)
        self.note("agent", frame=m)

    # ------------------------------------------------------------------ the loop

    async def _run(self) -> None:
        """The physics and the motion sensors, in real time: a tick owed is a tick run, even
        when the page or the camera kept the loop busy, so the robot's clock never drifts
        from the page's."""
        page = self.page
        start = time.monotonic()
        n = 0
        while True:
            due = int((time.monotonic() - start) / CONTROL_DT) + 1
            while n < due:
                try:
                    self.body.tick()
                    self.chip.tick()
                except Exception as e:
                    # A dead physics loop would read as a robot that froze: say why.
                    self.note("sim_error", error=repr(e)[:300])
                    raise
                n += 1
                if n % 2 == 0:
                    self._record()
            try:
                await page.evaluate("m => window.__simMotion(m)", self.body.motion())
                if time.monotonic() - self._drained > 1.0:
                    self._drained = time.monotonic()
                    await self._drain()
            except Exception as e:  # the page navigated or closed: the next tick tries again
                self.note("loop", error=str(e)[:200])
                await asyncio.sleep(0.2)
            await asyncio.sleep(max(0.0, start + n * CONTROL_DT - time.monotonic()))

    def _record(self) -> None:
        st = self.body.state()
        st["wall"] = self.now()
        st["pose"] = self.chip.pose
        st["qpos"] = self.body.data.qpos.tolist()
        st["person_at"] = self.body.data.mocap_pos.tolist()
        self.states.append(st)

    async def _camera(self) -> None:
        """The front camera, as fast as the renderer goes up to its cap: drawn in a thread
        from a copy of the scene, so the physics never waits on a picture."""
        import mujoco

        m = self.body.model
        snap = mujoco.MjData(m)
        renderer = None
        page = self.page
        n = 0

        def draw(qpos: Any, mocap_pos: Any, mocap_quat: Any) -> bytes:
            nonlocal renderer
            import imageio.v3 as iio

            if renderer is None:
                renderer = mujoco.Renderer(m, self.sb.CAM_H, self.sb.CAM_W)
            snap.qpos[:], snap.mocap_pos[:], snap.mocap_quat[:] = qpos, mocap_pos, mocap_quat
            mujoco.mj_kinematics(m, snap)
            mujoco.mj_camlight(m, snap)
            renderer.update_scene(snap, camera="front")
            return iio.imwrite("<bytes>", renderer.render(), extension=".jpg")

        while True:
            t = time.monotonic()
            d = self.body.data
            try:
                jpeg = await asyncio.to_thread(
                    draw, d.qpos.copy(), d.mocap_pos.copy(), d.mocap_quat.copy()
                )
                if n % 8 == 0:
                    self.frames.append((self.now(), jpeg))
                await page.evaluate("b => window.__simFrame(b)", base64.b64encode(jpeg).decode())
            except Exception as e:
                self.note("camera", error=str(e)[:200])
                await asyncio.sleep(0.5)
            n += 1
            await asyncio.sleep(max(0.0, 1.0 / self.o.fps - (time.monotonic() - t)))

    # ------------------------------------------------------------------ what a scenario does

    async def say(self, text: str) -> None:
        """Speak to Dave: the page gets it as its speech-to-text's answer."""
        self.note("you", text=text)
        await self.page.evaluate("t => heardText(t, {}, 'en')", text)

    async def wait(self, secs: float) -> None:
        await asyncio.sleep(secs)

    async def until(
        self, cond: Callable[[Sim], bool], timeout: float, what: str, every: float = 0.2
    ) -> bool:
        """Wait for `cond` up to `timeout` seconds, and record it as a check."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond(self):
                return self.check(True, what)
            await asyncio.sleep(every)
        return self.check(False, what, timeout_s=timeout)

    def push(self, force: tuple[float, float, float], secs: float = 0.1) -> None:
        self.note("push", force=list(force), secs=secs)
        self.body.push(force, secs)

    def person(self, **kw: Any) -> None:
        self.note("person", **kw)
        self.body.place_person(self.sb.Person(**kw))

    async def move(self, move: str, secs: float = 5, **extra: Any) -> dict[str, Any] | None:
        """A Body tab move, sent the way the box sends it on the side channel."""
        if self._agent_ws is None:
            return None
        rid = f"sim{len(self._acks)}{int(time.monotonic() * 1000) % 100000}"
        frame = {"t": "box_move", "rid": rid, "move": move, "secs": secs, **extra}
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._acks[rid] = fut
        self.note("move", frame=frame)
        self._agent_ws.send(json.dumps(frame))
        try:
            return await asyncio.wait_for(fut, 3.0)
        except TimeoutError:
            self._acks.pop(rid, None)
            return None

    async def _drain(self) -> None:
        """Move the page's own event log into the timeline, on the run's clock."""
        try:
            evs = await self.page.evaluate("() => window.__simEvents.splice(0)")
        except Exception:
            return
        for ev in evs:
            t = round(ev.get("t", 0) / 1000 - self.epoch0, 2)
            self.timeline.append(
                {"t": t, "kind": "page", "event": ev.get("kind"), "data": ev.get("data")}
            )

    async def close(self) -> Path:
        from dave_sim import report

        await self._drain()
        try:
            await self.page.screenshot(path=str(self.o.out / "page.png"))
        except Exception:
            pass
        tasks = [t for t in (self._loop, self._cam) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._pw is not None:
            await self._pw.stop()
        return report.write(self)
