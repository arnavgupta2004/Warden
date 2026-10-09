class PayPalError(Exception):
    def __init__(self, status: int, name: str, message: str, debug_id: str | None = None):
        super().__init__(f"{status} {name}: {message}")
        self.status = status
        self.name = name
        self.message = message
        self.debug_id = debug_id

    def as_dict(self) -> dict:
        return {"error": self.name, "status": self.status, "message": self.message, "debug_id": self.debug_id}
