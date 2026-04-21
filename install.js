module.exports = {
  run: [
    {
      method: "shell.run",
      params: {
        path: ".",
        message: "Creating virtual environment",
        command: "py -m venv .venv"
      }
    },
    {
      method: "shell.run",
      params: {
        path: ".",
        message: "Installing Python dependencies",
        command: ".\\.venv\\Scripts\\python -m pip install --upgrade pip ; .\\.venv\\Scripts\\python -m pip install -r requirements.txt"
      }
    }
  ]
};
