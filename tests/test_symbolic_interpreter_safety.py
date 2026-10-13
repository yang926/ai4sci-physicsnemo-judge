"""Reject expensive expressions at interpretation and every residual comparison."""
import subprocess
import sys
import textwrap

import pytest
import sympy as sp

from ai4sci_judge.evaluate import check_equations, load_lesson
from ai4sci_judge.expressions import Interpreter, SubmissionError, build
from ETC.runtime.symbolic_checks import equivalent_expression, validate_expression
from tests.test_judge import answer
from tests.test_judge_operators import check, operator_answer


x, y = sp.symbols("x y")


def fail_transform(*args, **kwargs):
    pytest.fail("Unsafe expressions must fail before rewrite or expansion")


@pytest.mark.parametrize("body", [
    'q = sin(x)+sin(y)+sin(x*y)+sin(x*x)+sin(y*y)+sin(x*y*y)\n    return {"c": (q**-2)**2}',
    'q = (x+y)**-2\n    q = q**2\n    return {"c": q**2}',
    'q = x\n' + '    q = sin(q)\n'*15 + '    return {"c": q}',
])
def test_all_intermediates_are_bounded_before_transformation(body, monkeypatch):
    function = build('def student_speed(x,y):\n    ' + body,
                     {"x": None, "y": None}, name="student_speed", data=True)
    monkeypatch.setattr(sp.Expr, "rewrite", fail_transform)
    monkeypatch.setattr(sp, "expand", fail_transform)
    with pytest.raises(SubmissionError, match="budget|powers|complex"):
        function(x, y)


def test_tiny_but_huge_denominators_are_rejected_before_transform(monkeypatch):
    monkeypatch.setattr(sp.Expr, "rewrite", fail_transform)
    monkeypatch.setattr(sp, "expand", fail_transform)
    with pytest.raises(ValueError, match="numeric budget"):
        validate_expression(sp.Rational(1, 1 << 5000))


def test_huge_integer_literal_is_a_submission_error_not_float_overflow():
    source = 'def student_speed(x,y):\n    return {"c": ' + "9"*1000 + '}\n'
    function = build(source, {"x": None, "y": None}, name="student_speed", data=True)
    with pytest.raises(SubmissionError):
        function(x, y)


def test_derivative_work_is_bounded_before_diff(monkeypatch):
    # This finite input has modest expansion cost, but two product/chain-rule
    # derivatives would exceed the explicit interpreter operation budget.
    expression = sum(sp.exp(index*x) for index in range(1, 24))
    interpreter = Interpreter({"x": x, "y": y, "u": expression})
    import ast
    monkeypatch.setattr(sp.Expr, "diff", fail_transform)
    with pytest.raises(SubmissionError, match="Derivative expansion"):
        interpreter.value(ast.parse("u.diff(x,2)", mode="eval").body)


@pytest.mark.parametrize("challenge,filename,residual", [
    ("1", "wave_l1.py", "wave"),
    ("2", "chip_2d_l1.py", "continuity"),
    ("3", "climate_l1.py", "adr"),
])
def test_pde_entry_points_reject_reciprocals_without_expansion(challenge, filename, residual, monkeypatch):
    module = load_lesson(challenge, filename)
    source = answer(challenge, filename)
    import ast
    tree = ast.parse(source)
    equation = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "student_equations")
    start, stop = equation.lineno - 1, equation.end_lineno
    lines = source.splitlines()
    signature = lines[start]
    replacement = signature + '\n    q = sin(x)+sin(y)+sin(x*y)+sin(x*x)+sin(y*y)+sin(x*y*y)\n    return {"' + residual + '": (q**-2)**2}'
    source = "\n".join(lines[:start] + [replacement] + lines[stop:])
    monkeypatch.setattr(sp.Expr, "rewrite", fail_transform)
    monkeypatch.setattr(sp, "expand", fail_transform)
    with pytest.raises(SubmissionError, match="budget"):
        check_equations(module, source, challenge)


def test_pino_entry_point_rejects_reciprocal_without_expansion(monkeypatch):
    source = operator_answer(3).replace(
        '        self.equations =',
        '        q = sin(x)+sin(y)+sin(x*y)+sin(x*x)+sin(y*y)+sin(x*y*y)\n        self.equations =',
    ).replace('u - u.diff(x, 2) - u.diff(y, 2) - f', '(q**-2)**2')
    # Import trusted training modules before forbidding symbolic operations.
    load_lesson("4", "fno_physicsnemo_l3.py")
    monkeypatch.setattr(sp.Expr, "rewrite", fail_transform)
    monkeypatch.setattr(sp, "expand", fail_transform)
    result = check(source, 3)
    assert result["checks"]["physics"] is False
    assert "budget" in result["messages"]["physics"]


def test_residual_sign_and_scale_are_not_equivalent():
    u = sp.Function("u")(x, y)
    reference = u - u.diff(x, 2) - u.diff(y, 2)
    assert equivalent_expression(reference, reference)
    assert not equivalent_expression(-reference, reference)
    assert not equivalent_expression(2*reference, reference)
    assert not equivalent_expression(0, reference)


def test_repeated_numeric_reciprocal_reproduction_is_resource_isolated():
    script = textwrap.dedent('''
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024**2, 384 * 1024**2))
        import sympy as sp
        from ai4sci_judge.expressions import build, SubmissionError
        body = "    q = 0.1\\n" + "    q = q**2\\n" * 16
        source = "def student_speed(x,y):\\n" + body + "    return {'c': q}\\n"
        fn = build(source, {'x': None, 'y': None}, name='student_speed', data=True, exact_numbers=True)
        try:
            fn(*sp.symbols('x y'))
        except SubmissionError as error:
            assert 'numeric budget' in str(error), str(error)
            print('bounded rejection')
        else:
            raise AssertionError('Expected numeric representation guard')
    ''')
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=10, check=True)
    assert result.stdout.strip() == "bounded rejection"
