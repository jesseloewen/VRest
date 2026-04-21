module.exports = {
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        path: ".",
        message: "Starting VRest",
        command: ".\\.venv\\Scripts\\python app.py"
      }
    }
  ]
};
