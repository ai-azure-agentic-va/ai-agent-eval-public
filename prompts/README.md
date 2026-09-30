# Judge prompts

Edit judge instructions here. The Python judges handle input formatting, model
calls and response parsing.

- Each metric has one `.txt` prompt directly in this directory, using 0–1 scores.
- `instructions.txt`: shared instructions prepended to every judge prompt.
- `fallback_reference.txt`: optional reference block used by the fallback judge.
- `__init__.py`: loads the UTF-8 files once as string constants, relative to this
  directory rather than the process's working directory.

Templates use Python `str.format` placeholders such as `{question}`, `{response}`,
`{reference}` and `{context}`. Keep literal JSON braces doubled (`{{` and `}}`).
Existing whitespace is preserved. Restart the service after editing a prompt.

Historical 1–5 results remain readable. To evaluate their datasets again, start a
new run with the current prompts; legacy runs cannot be resumed or rejudged.

The Docker build copies this directory with the application. Keep it alongside
`service/` when running or packaging the application.
