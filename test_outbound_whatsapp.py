import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch
import httpx
import outbound_whatsapp

TOKEN = "super-secret-test-token-value"


class SendTextMessageTests(unittest.TestCase):
    # 7. outbound WhatsApp API mocked successfully
    def test_successful_send_returns_provider_message_id(self):
        response = httpx.Response(200, json={"messages": [{"id": "wamid.OUT123"}]})
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": TOKEN}), \
             patch("outbound_whatsapp.httpx.AsyncClient") as factory:
            client = AsyncMock()
            client.post = AsyncMock(return_value=response)
            factory.return_value.__aenter__.return_value = client
            result = asyncio.run(outbound_whatsapp.send_text_message("phone-id-1", "234800", "hello"))
        self.assertEqual(result["provider_message_id"], "wamid.OUT123")
        sent_url = client.post.call_args.args[0]
        self.assertIn("phone-id-1", sent_url)
        sent_headers = client.post.call_args.kwargs["headers"]
        self.assertEqual(sent_headers["Authorization"], "Bearer " + TOKEN)

    # 8. failed outbound API response recorded safely
    def test_non_200_raises_outbound_unavailable(self):
        response = httpx.Response(400, json={"error": "bad request"})
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": TOKEN}), \
             patch("outbound_whatsapp.httpx.AsyncClient") as factory:
            client = AsyncMock()
            client.post = AsyncMock(return_value=response)
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(outbound_whatsapp.OutboundUnavailable) as ctx:
                asyncio.run(outbound_whatsapp.send_text_message("phone-id-1", "234800", "hello"))
        self.assertNotIn(TOKEN, str(ctx.exception))

    # 14. missing WHATSAPP_ACCESS_TOKEN fails safely
    def test_missing_token_fails_safely(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(outbound_whatsapp.OutboundUnavailable):
                asyncio.run(outbound_whatsapp.send_text_message("phone-id-1", "234800", "hello"))

    # 15. secret never appears in logs/errors
    def test_token_never_appears_in_any_exception(self):
        with patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": TOKEN}), \
             patch("outbound_whatsapp.httpx.AsyncClient") as factory:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(outbound_whatsapp.OutboundUnavailable) as ctx:
                asyncio.run(outbound_whatsapp.send_text_message("phone-id-1", "234800", "hello"))
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_missing_token_error_does_not_echo_empty_env_value(self):
        with patch.dict(os.environ, {}, clear=True):
            try:
                asyncio.run(outbound_whatsapp.send_text_message("phone-id-1", "234800", "hello"))
            except outbound_whatsapp.OutboundUnavailable as error:
                self.assertNotIn("WHATSAPP_ACCESS_TOKEN=", str(error))


if __name__ == "__main__":
    unittest.main()
