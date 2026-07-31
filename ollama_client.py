import json
import re

import ollama


class OllamaClient:

    def __init__(self, model):
        self.model = model

    def ask(self, prompt, system=None):

        messages = []

        if system:
            messages.append({"role": "system", "content": system})

        messages.append({"role": "user", "content": prompt})

        response = ollama.chat(
            model=self.model,
            messages=messages
        )

        return response["message"]["content"]

    def ask_json(self, prompt, system=None, retries=2):
        """Calls the model and parses the reply as JSON, retrying and
        stripping markdown code fences if the model doesn't comply cleanly."""

        last_error = None
        last_raw = None

        for _ in range(retries + 1):

            raw = self.ask(prompt, system=system)
            last_raw = raw

            cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()

            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as e:
                last_error = e
                prompt = (
                    "Your previous reply was not valid JSON and raised this "
                    f"error: {e}. Reply again with ONLY the corrected valid "
                    "JSON object, no other text."
                )

        raise ValueError(
            f"Model did not return valid JSON after retries. Last error: {last_error}. "
            f"Last raw response: {last_raw!r}"
        )
