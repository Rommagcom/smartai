from abc import ABC, abstractmethod


class MessengerAdapter(ABC):
    @abstractmethod
    async def run(self) -> None:
        raise NotImplementedError
