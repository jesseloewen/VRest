module.exports = {
  run: [
    {
      method: "shell.run",
      params: {
        path: ".",
        message: [
          "py -m venv venv"
        ]
      }
    },
    {
      method: "shell.run",
      params: {
        path: ".",
        venv: "venv",
        message: [
          "python -m pip install --upgrade pip",
          "python -m pip install -r requirements.txt"
        ]
      }
    }
  ]
};
