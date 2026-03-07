"""Tests for the PII Evaluator."""

import pytest
from unittest.mock import MagicMock
from evalview.evaluators.pii_evaluator import PIIEvaluator, _luhn_check


# ---------------------------------------------------------------------------
# Luhn algorithm unit tests
# ---------------------------------------------------------------------------

def test_luhn_valid_visa():
    assert _luhn_check("4111111111111111") is True

def test_luhn_valid_mastercard():
    assert _luhn_check("5500000000000004") is True

def test_luhn_invalid():
    assert _luhn_check("1234567890123456") is False

def test_luhn_single_digit():
    assert _luhn_check("0") is True


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make(output: str):
    trace = MagicMock()
    trace.final_output = output
    return MagicMock(), trace


# ---------------------------------------------------------------------------
# Clean text — should pass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_passes_clean_text():
    evaluator = PIIEvaluator()
    tc, trace = _make("Hello world, the weather is nice today.")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert result.passed is True
    assert result.has_pii is False
    assert len(result.types_detected) == 0


@pytest.mark.asyncio
async def test_passes_empty_output():
    evaluator = PIIEvaluator()
    tc, trace = _make("")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert result.passed is True
    assert result.has_pii is False


# ---------------------------------------------------------------------------
# Real PII — should fail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detects_email():
    evaluator = PIIEvaluator()
    tc, trace = _make("Contact john.doe@example.com for details.")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert result.passed is False
    assert "email" in result.types_detected


@pytest.mark.asyncio
async def test_detects_phone_with_parens():
    evaluator = PIIEvaluator()
    tc, trace = _make("Call us at (123) 456-7890.")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert result.passed is False
    assert "phone" in result.types_detected


@pytest.mark.asyncio
async def test_detects_phone_with_country_code():
    evaluator = PIIEvaluator()
    tc, trace = _make("Reach me at +1-555-123-4567.")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert result.passed is False
    assert "phone" in result.types_detected


@pytest.mark.asyncio
async def test_detects_ssn():
    evaluator = PIIEvaluator()
    tc, trace = _make("SSN: 123-45-6789")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert result.passed is False
    assert "ssn" in result.types_detected


@pytest.mark.asyncio
async def test_detects_valid_credit_card():
    evaluator = PIIEvaluator()
    # 4111 1111 1111 1111 passes Luhn
    tc, trace = _make("Card: 4111 1111 1111 1111")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert result.passed is False
    assert "credit_card" in result.types_detected


@pytest.mark.asyncio
async def test_detects_address():
    evaluator = PIIEvaluator()
    tc, trace = _make("Ship to 123 Main Street")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert result.passed is False
    assert "address" in result.types_detected


@pytest.mark.asyncio
async def test_detects_multiple_types():
    evaluator = PIIEvaluator()
    tc, trace = _make(
        "Email john@test.com or call (555) 123-4567."
    )
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert result.passed is False
    assert "email" in result.types_detected
    assert "phone" in result.types_detected


# ---------------------------------------------------------------------------
# False positive reduction — should pass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plain_digits_not_phone():
    """Plain 10-digit numbers without formatting should not trigger phone detection."""
    evaluator = PIIEvaluator()
    tc, trace = _make("Order number: 1234567890. Reference: 9876543210.")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert "phone" not in result.types_detected


@pytest.mark.asyncio
async def test_invalid_credit_card_fails_luhn():
    """16-digit number that fails Luhn should not be flagged as credit card."""
    evaluator = PIIEvaluator()
    tc, trace = _make("ID: 1234-5678-9012-3456")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert "credit_card" not in result.types_detected


@pytest.mark.asyncio
async def test_invalid_ssn_ranges():
    """SSNs starting with 000, 666, or 9xx are invalid and should not match."""
    evaluator = PIIEvaluator()
    tc, trace = _make("Code: 000-12-3456 and 666-45-6789 and 900-11-2222")
    result = await evaluator.evaluate(test_case=tc, trace=trace)
    assert "ssn" not in result.types_detected
