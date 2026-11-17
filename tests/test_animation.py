"""Deterministic playback checks; no UI, sleeps, or generated replacement art."""
import unittest

from shu.pet_core import Animation, STATES


class AnimationPlaybackTests(unittest.TestCase):
    def test_repeated_same_direction_drag_requests_do_not_pin_first_frame(self):
        for state in ("running-left", "running-right"):
            with self.subTest(state=state):
                animation = Animation(100.0)
                animation.play(state, 100.0, loop=True)
                visited = {animation.frame}
                # A drag emits approximately 60 movement requests each second,
                # much faster than the sprite's 120 ms frame interval.
                for sample in range(1, 110):
                    now = 100.0 + sample / 60
                    animation.play(state, now, loop=True)
                    animation.advance(now)
                    visited.add(animation.frame)
                self.assertEqual(visited, set(range(8)))
                self.assertEqual(animation.state, state)
                self.assertTrue(animation.loop)

    def test_each_direction_loops_after_eight_uniform_frames_without_tail_hold(self):
        for state in ("running-left", "running-right"):
            with self.subTest(state=state):
                animation = Animation(0.0)
                animation.play(state, 0.0, loop=True)
                for frame in range(8):
                    animation.advance(frame * 0.12 + 0.001)
                    self.assertEqual(animation.frame, frame)
                animation.advance(0.959)
                self.assertEqual(animation.frame, 7)
                animation.advance(0.961)
                self.assertEqual(animation.frame, 0)
                animation.advance(1.081)
                self.assertEqual(animation.frame, 1)
                self.assertEqual(animation.cell[0], STATES[state][0])

    def test_switching_direction_restarts_once_then_keeps_its_new_clock(self):
        animation = Animation(0.0)
        animation.play("running-right", 0.0, loop=True)
        animation.advance(0.37)
        self.assertEqual(animation.cell, (1, 3))
        animation.play("running-left", 0.4, loop=True)
        self.assertEqual(animation.cell, (2, 0))
        self.assertAlmostEqual(animation.deadline, 0.52)
        animation.play("running-left", 0.45, loop=True)
        self.assertAlmostEqual(animation.deadline, 0.52)
        animation.advance(0.53)
        self.assertEqual(animation.cell, (2, 1))

    def test_stopping_run_returns_to_idle_and_does_not_resume_running(self):
        animation = Animation(0.0)
        animation.play("running-left", 0.0, loop=True)
        animation.advance(0.13)
        animation.play("idle", 0.2, loop=True)
        self.assertEqual(animation.cell, (0, 0))
        animation.advance(0.479)
        self.assertEqual(animation.cell, (0, 0))
        animation.advance(0.481)
        self.assertEqual(animation.cell, (0, 1))
        animation.advance(1.4)
        self.assertEqual(animation.state, "idle")

    def test_idle_keeps_original_frame_timings(self):
        animation = Animation(0.0)
        for now, frame in ((0.279, 0), (0.281, 1), (0.391, 2), (0.501, 3),
                           (0.641, 4), (0.781, 5), (1.101, 0)):
            animation.advance(now)
            self.assertEqual(animation.frame, frame)
        self.assertEqual(animation.state, "idle")

    def test_one_shot_actions_still_replay_and_return_to_idle(self):
        animation = Animation(0.0)
        animation.play("waving", 0.0)
        animation.advance(0.15)
        self.assertEqual(animation.cell, (3, 1))
        # Unlike a repeated looping request, an explicit one-shot action is a
        # deliberate replay and should restart its first frame.
        animation.play("waving", 0.2)
        self.assertEqual(animation.cell, (3, 0))
        self.assertAlmostEqual(animation.deadline, 0.34)
        animation.advance(0.901)
        self.assertEqual(animation.cell, (0, 0))
        self.assertTrue(animation.loop)


if __name__ == "__main__":
    unittest.main()
