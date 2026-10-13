"""The standalone grader must use the same bounded exact setup checks."""
import pytest

from ai4sci_judge.contracts import check_setup
from ai4sci_judge.evaluate import load_lesson
from tests.test_judge import answer


def test_standalone_accepts_equivalent_wave_speed():
    source = answer("1", "wave_l2.py").replace(
        '1 + .5 * sin(x) * cos(y)', '1 + .25*(sin(x+y) + sin(x-y))')
    _, checks = check_setup(load_lesson("1", "wave_l2.py"), source, "1")
    assert all(checks.values())


@pytest.mark.parametrize("filename", ["climate_l1.py", "climate_l2.py"])
def test_standalone_accepts_equivalent_conditions_and_analytic_solution(filename):
    source = answer("3", filename).replace('sin(x)*sin(y)', '(cos(x-y)-cos(x+y))/2')
    _, checks = check_setup(load_lesson("3", filename), source, "3")
    assert all(checks.values())


def test_standalone_does_not_accept_close_speed():
    source = answer("1", "wave_l2.py").replace(
        '1 + .5 * sin(x) * cos(y)', '1 + .25*(sin(x+y) + sin(x-y)) + 1e-12*x')
    _, checks = check_setup(load_lesson("1", "wave_l2.py"), source, "1")
    assert checks["parameter.wave_speed"] is False
