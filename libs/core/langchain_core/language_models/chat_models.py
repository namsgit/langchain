from __future__ import annotations

import asyncio
import inspect
import warnings
from abc import ABC, abstractmethod
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    cast,
)

from langchain_core._api import deprecated
from langchain_core.callbacks import (
    AsyncCallbackManager,
    AsyncCallbackManagerForLLMRun,
    BaseCallbackManager,
    CallbackManager,
    CallbackManagerForLLMRun,
    Callbacks,
)
from langchain_core.globals import get_llm_cache
from langchain_core.language_models.base import BaseLanguageModel, LanguageModelInput
from langchain_core.load import dumpd, dumps
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    BaseMessageChunk,
    HumanMessage,
    convert_to_messages,
    message_chunk_to_message,
)
from langchain_core.outputs import (
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
    LLMResult,
    RunInfo,
)
from langchain_core.prompt_values import ChatPromptValue, PromptValue, StringPromptValue
from langchain_core.pydantic_v1 import Field, root_validator
from langchain_core.runnables.config import ensure_config, run_in_executor

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


def _get_verbosity() -> bool:
    from langchain_core.globals import get_verbose

    return get_verbose()


def generate_from_stream(stream: Iterator[ChatGenerationChunk]) -> ChatResult:
    """Generate from a stream."""

    generation: Optional[ChatGenerationChunk] = None
    for chunk in stream:
        if generation is None:
            generation = chunk
        else:
            generation += chunk
    assert generation is not None
    return ChatResult(
        generations=[
            ChatGeneration(
                message=message_chunk_to_message(generation.message),
                generation_info=generation.generation_info,
            )
        ]
    )


async def agenerate_from_stream(
    stream: AsyncIterator[ChatGenerationChunk],
) -> ChatResult:
    """Async generate from a stream."""

    generation: Optional[ChatGenerationChunk] = None
    async for chunk in stream:
        if generation is None:
            generation = chunk
        else:
            generation += chunk
    assert generation is not None
    return ChatResult(
        generations=[
            ChatGeneration(
                message=message_chunk_to_message(generation.message),
                generation_info=generation.generation_info,
            )
        ]
    )


def _as_async_iterator(sync_iterator: Callable) -> Callable:
    """Convert a sync iterator into an async iterator."""

    async def _as_sync_iterator(*args: Any, **kwargs: Any) -> AsyncGenerator:
        iterator = await run_in_executor(None, sync_iterator, *args, **kwargs)
        done = object()
        while True:
            item = await run_in_executor(
                None,
                next,
                iterator,
                done,  # type: ignore[call-arg, arg-type]
            )
            if item is done:
                break
            yield item  # type: ignore[misc]

    return _as_sync_iterator


class BaseChatModel(BaseLanguageModel[BaseMessage], ABC):
    """Base class for Chat models."""

    cache: Optional[bool] = None
    """Whether to cache the response."""
    verbose: bool = Field(default_factory=_get_verbosity)
    """Whether to print out response text."""
    callbacks: Callbacks = Field(default=None, exclude=True)
    """Callbacks to add to the run trace."""
    callback_manager: Optional[BaseCallbackManager] = Field(default=None, exclude=True)
    """[DEPRECATED] Callback manager to add to the run trace."""
    tags: Optional[List[str]] = Field(default=None, exclude=True)
    """Tags to add to the run trace."""
    metadata: Optional[Dict[str, Any]] = Field(default=None, exclude=True)
    """Metadata to add to the run trace."""

    @root_validator()
    def raise_deprecation(cls, values: Dict) -> Dict:
        """Raise deprecation warning if callback_manager is used."""
        if values.get("callback_manager") is not None:
            warnings.warn(
                "callback_manager is deprecated. Please use callbacks instead.",
                DeprecationWarning,
            )
            values["callbacks"] = values.pop("callback_manager", None)
        return values

    class Config:
        """Configuration for this pydantic object."""

        arbitrary_types_allowed = True

    # --- Runnable methods ---

    @property
    def OutputType(self) -> Any:
        """Get the output type for this runnable."""
        return AnyMessage

    def _convert_input(self, input: LanguageModelInput) -> PromptValue:
        if isinstance(input, PromptValue):
            return input
        elif isinstance(input, str):
            return StringPromptValue(text=input)
        elif isinstance(input, Sequence):
            return ChatPromptValue(messages=convert_to_messages(input))
        else:
            raise ValueError(
                f"Invalid input type {type(input)}. "
                "Must be a PromptValue, str, or list of BaseMessages."
            )

    def invoke(
        self,
        input: LanguageModelInput,
        config: Optional[RunnableConfig] = None,
        *,
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> BaseMessage:
        config = ensure_config(config)
        return cast(
            ChatGeneration,
            self.generate_prompt(
                [self._convert_input(input)],
                stop=stop,
                callbacks=config.get("callbacks"),
                tags=config.get("tags"),
                metadata=config.get("metadata"),
                run_name=config.get("run_name"),
                **kwargs,
            ).generations[0][0],
        ).message

    async def ainvoke(
        self,
        input: LanguageModelInput,
        config: Optional[RunnableConfig] = None,
        *,
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> BaseMessage:
        config = ensure_config(config)
        llm_result = await self.agenerate_prompt(
            [self._convert_input(input)],
            stop=stop,
            callbacks=config.get("callbacks"),
            tags=config.get("tags"),
            metadata=config.get("metadata"),
            run_name=config.get("run_name"),
            **kwargs,
        )
        return cast(ChatGeneration, llm_result.generations[0][0]).message

    def stream(
        self,
        input: LanguageModelInput,
        config: Optional[RunnableConfig] = None,
        *,
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Iterator[BaseMessageChunk]:
        if type(self)._stream == BaseChatModel._stream:
            # model doesn't implement streaming, so use default implementation
            yield cast(
                BaseMessageChunk, self.invoke(input, config=config, stop=stop, **kwargs)
            )
        else:
            config = ensure_config(config)
            messages = self._convert_input(input).to_messages()
            params = self._get_invocation_params(stop=stop, **kwargs)
            options = {"stop": stop, **kwargs}
            callback_manager = CallbackManager.configure(
                config.get("callbacks"),
                self.callbacks,
                self.verbose,
                config.get("tags"),
                self.tags,
                config.get("metadata"),
                self.metadata,
            )
            (run_manager,) = callback_manager.on_chat_model_start(
                dumpd(self),
                [messages],
                invocation_params=params,
                options=options,
                name=config.get("run_name"),
                batch_size=1,
            )
            generation: Optional[ChatGenerationChunk] = None
            try:
                for chunk in self._stream(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                ):
                    yield chunk.message
                    if generation is None:
                        generation = chunk
                    else:
                        generation += chunk
                assert generation is not None
            except BaseException as e:
                run_manager.on_llm_error(
                    e,
                    response=LLMResult(
                        generations=[[generation]] if generation else []
                    ),
                )
                raise e
            else:
                run_manager.on_llm_end(LLMResult(generations=[[generation]]))

    async def astream(
        self,
        input: LanguageModelInput,
        config: Optional[RunnableConfig] = None,
        *,
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessageChunk]:
        if type(self)._astream is not BaseChatModel._astream:
            # Then astream is implemented
            _stream_implementation = self._astream
        elif type(self)._stream is not BaseChatModel._stream:
            # Then stream is implemented, so we can create an async iterator from it
            # The typing is hard to type correctly with mypy here, so we cast
            # and do a type ignore, this code is unit tested and should be fine.
            _stream_implementation = cast(  # type: ignore
                Callable[
                    [
                        List[BaseMessage],
                        Optional[List[str]],
                        CallbackManagerForLLMRun,
                        Any,
                    ],
                    AsyncIterator[ChatGenerationChunk],
                ],
                _as_async_iterator(self._stream),
            )
        else:  # No async or sync stream is implemented, so fall back to ainvoke
            yield cast(
                BaseMessageChunk,
                await self.ainvoke(input, config=config, stop=stop, **kwargs),
            )
            return

        config = ensure_config(config)
        messages = self._convert_input(input).to_messages()
        params = self._get_invocation_params(stop=stop, **kwargs)
        options = {"stop": stop, **kwargs}
        callback_manager = AsyncCallbackManager.configure(
            config.get("callbacks"),
            self.callbacks,
            self.verbose,
            config.get("tags"),
            self.tags,
            config.get("metadata"),
            self.metadata,
        )
        (run_manager,) = await callback_manager.on_chat_model_start(
            dumpd(self),
            [messages],
            invocation_params=params,
            options=options,
            name=config.get("run_name"),
            batch_size=1,
        )
        generation: Optional[ChatGenerationChunk] = None
        try:
            async for chunk in _stream_implementation(
                messages, stop=stop, run_manager=run_manager, **kwargs
            ):
                yield chunk.message
                if generation is None:
                    generation = chunk
                else:
                    generation += chunk
            assert generation is not None
        except BaseException as e:
            await run_manager.on_llm_error(
                e,
                response=LLMResult(generations=[[generation]] if generation else []),
            )
            raise e
        else:
            await run_manager.on_llm_end(
                LLMResult(generations=[[generation]]),
            )

    # --- Custom methods ---

    def _combine_llm_outputs(self, llm_outputs: List[Optional[dict]]) -> dict:
        return {}

    def _get_invocation_params(
        self,
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> dict:
        params = self.dict()
        params["stop"] = stop
        return {**params, **kwargs}

    def _get_llm_string(self, stop: Optional[List[str]] = None, **kwargs: Any) -> str:
        if self.is_lc_serializable():
            params = {**kwargs, **{"stop": stop}}
            param_string = str(sorted([(k, v) for k, v in params.items()]))
            llm_string = dumps(self)
            return llm_string + "---" + param_string
        else:
            params = self._get_invocation_params(stop=stop, **kwargs)
            params = {**params, **kwargs}
            return str(sorted([(k, v) for k, v in params.items()]))

    def generate(
        self,
        messages: List[List[BaseMessage]],
        stop: Optional[List[str]] = None,
        callbacks: Callbacks = None,
        *,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        run_name: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResult:
        """Pass a sequence of prompts to the model and return model generations.

        This method should make use of batched calls for models that expose a batched
        API.

        Use this method when you want to:
            1. take advantage of batched calls,
            2. need more output from the model than just the top generated value,
            3. are building chains that are agnostic to the underlying language model
                type (e.g., pure text completion models vs chat models).

        Args:
            messages: List of list of messages.
            stop: Stop words to use when generating. Model output is cut off at the
                first occurrence of any of these substrings.
            callbacks: Callbacks to pass through. Used for executing additional
                functionality, such as logging or streaming, throughout generation.
            **kwargs: Arbitrary additional keyword arguments. These are usually passed
                to the model provider API call.

        Returns:
            An LLMResult, which contains a list of candidate Generations for each input
                prompt and additional model provider-specific output.
        """
        params = self._get_invocation_params(stop=stop, **kwargs)
        options = {"stop": stop}

        callback_manager = CallbackManager.configure(
            callbacks,
            self.callbacks,
            self.verbose,
            tags,
            self.tags,
            metadata,
            self.metadata,
        )
        run_managers = callback_manager.on_chat_model_start(
            dumpd(self),
            messages,
            invocation_params=params,
            options=options,
            name=run_name,
            batch_size=len(messages),
        )
        results = []
        for i, m in enumerate(messages):
            try:
                results.append(
                    self._generate_with_cache(
                        m,
                        stop=stop,
                        run_manager=run_managers[i] if run_managers else None,
                        **kwargs,
                    )
                )
            except BaseException as e:
                if run_managers:
                    run_managers[i].on_llm_error(e, response=LLMResult(generations=[]))
                raise e
        flattened_outputs = [
            LLMResult(generations=[res.generations], llm_output=res.llm_output)  # type: ignore[list-item]
            for res in results
        ]
        llm_output = self._combine_llm_outputs([res.llm_output for res in results])
        generations = [res.generations for res in results]
        output = LLMResult(generations=generations, llm_output=llm_output)  # type: ignore[arg-type]
        if run_managers:
            run_infos = []
            for manager, flattened_output in zip(run_managers, flattened_outputs):
                manager.on_llm_end(flattened_output)
                run_infos.append(RunInfo(run_id=manager.run_id))
            output.run = run_infos
        return output

    async def agenerate(
        self,
        messages: List[List[BaseMessage]],
        stop: Optional[List[str]] = None,
        callbacks: Callbacks = None,
        *,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        run_name: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResult:
        """Asynchronously pass a sequence of prompts to a model and return generations.

        This method should make use of batched calls for models that expose a batched
        API.

        Use this method when you want to:
            1. take advantage of batched calls,
            2. need more output from the model than just the top generated value,
            3. are building chains that are agnostic to the underlying language model
                type (e.g., pure text completion models vs chat models).

        Args:
            messages: List of list of messages.
            stop: Stop words to use when generating. Model output is cut off at the
                first occurrence of any of these substrings.
            callbacks: Callbacks to pass through. Used for executing additional
                functionality, such as logging or streaming, throughout generation.
            **kwargs: Arbitrary additional keyword arguments. These are usually passed
                to the model provider API call.

        Returns:
            An LLMResult, which contains a list of candidate Generations for each input
                prompt and additional model provider-specific output.
        """
        params = self._get_invocation_params(stop=stop, **kwargs)
        options = {"stop": stop}

        callback_manager = AsyncCallbackManager.configure(
            callbacks,
            self.callbacks,
            self.verbose,
            tags,
            self.tags,
            metadata,
            self.metadata,
        )

        run_managers = await callback_manager.on_chat_model_start(
            dumpd(self),
            messages,
            invocation_params=params,
            options=options,
            name=run_name,
            batch_size=len(messages),
        )

        results = await asyncio.gather(
            *[
                self._agenerate_with_cache(
                    m,
                    stop=stop,
                    run_manager=run_managers[i] if run_managers else None,
                    **kwargs,
                )
                for i, m in enumerate(messages)
            ],
            return_exceptions=True,
        )
        exceptions = []
        for i, res in enumerate(results):
            if isinstance(res, BaseException):
                if run_managers:
                    await run_managers[i].on_llm_error(
                        res, response=LLMResult(generations=[])
                    )
                exceptions.append(res)
        if exceptions:
            if run_managers:
                await asyncio.gather(
                    *[
                        run_manager.on_llm_end(
                            LLMResult(
                                generations=[res.generations],  # type: ignore[list-item, union-attr]
                                llm_output=res.llm_output,  # type: ignore[list-item, union-attr]
                            )
                        )
                        for run_manager, res in zip(run_managers, results)
                        if not isinstance(res, Exception)
                    ]
                )
            raise exceptions[0]
        flattened_outputs = [
            LLMResult(generations=[res.generations], llm_output=res.llm_output)  # type: ignore[list-item, union-attr]
            for res in results
        ]
        llm_output = self._combine_llm_outputs([res.llm_output for res in results])  # type: ignore[union-attr]
        generations = [res.generations for res in results]  # type: ignore[union-attr]
        output = LLMResult(generations=generations, llm_output=llm_output)  # type: ignore[arg-type]
        await asyncio.gather(
            *[
                run_manager.on_llm_end(flattened_output)
                for run_manager, flattened_output in zip(
                    run_managers, flattened_outputs
                )
            ]
        )
        if run_managers:
            output.run = [
                RunInfo(run_id=run_manager.run_id) for run_manager in run_managers
            ]
        return output

    def generate_prompt(
        self,
        prompts: List[PromptValue],
        stop: Optional[List[str]] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> LLMResult:
        prompt_messages = [p.to_messages() for p in prompts]
        return self.generate(prompt_messages, stop=stop, callbacks=callbacks, **kwargs)

    async def agenerate_prompt(
        self,
        prompts: List[PromptValue],
        stop: Optional[List[str]] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> LLMResult:
        prompt_messages = [p.to_messages() for p in prompts]
        return await self.agenerate(
            prompt_messages, stop=stop, callbacks=callbacks, **kwargs
        )

    def _generate_with_cache(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        new_arg_supported = inspect.signature(self._generate).parameters.get(
            "run_manager"
        )
        disregard_cache = self.cache is not None and not self.cache
        llm_cache = get_llm_cache()
        if llm_cache is None or disregard_cache:
            # This happens when langchain.cache is None, but self.cache is True
            if self.cache is not None and self.cache:
                raise ValueError(
                    "Asked to cache, but no cache found at `langchain.cache`."
                )
            if new_arg_supported:
                return self._generate(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
            else:
                return self._generate(messages, stop=stop, **kwargs)
        else:
            llm_string = self._get_llm_string(stop=stop, **kwargs)
            prompt = dumps(messages)
            cache_val = llm_cache.lookup(prompt, llm_string)
            if isinstance(cache_val, list):
                return ChatResult(generations=cache_val)
            else:
                if new_arg_supported:
                    result = self._generate(
                        messages, stop=stop, run_manager=run_manager, **kwargs
                    )
                else:
                    result = self._generate(messages, stop=stop, **kwargs)
                llm_cache.update(prompt, llm_string, result.generations)
                return result

    async def _agenerate_with_cache(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        new_arg_supported = inspect.signature(self._agenerate).parameters.get(
            "run_manager"
        )
        disregard_cache = self.cache is not None and not self.cache
        llm_cache = get_llm_cache()
        if llm_cache is None or disregard_cache:
            # This happens when langchain.cache is None, but self.cache is True
            if self.cache is not None and self.cache:
                raise ValueError(
                    "Asked to cache, but no cache found at `langchain.cache`."
                )
            if new_arg_supported:
                return await self._agenerate(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
            else:
                return await self._agenerate(messages, stop=stop, **kwargs)
        else:
            llm_string = self._get_llm_string(stop=stop, **kwargs)
            prompt = dumps(messages)
            cache_val = await llm_cache.alookup(prompt, llm_string)
            if isinstance(cache_val, list):
                return ChatResult(generations=cache_val)
            else:
                if new_arg_supported:
                    result = await self._agenerate(
                        messages, stop=stop, run_manager=run_manager, **kwargs
                    )
                else:
                    result = await self._agenerate(messages, stop=stop, **kwargs)
                await llm_cache.aupdate(prompt, llm_string, result.generations)
                return result

    @abstractmethod
    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Top Level call"""

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Top Level call"""
        return await run_in_executor(
            None,
            self._generate,
            messages,
            stop,
            run_manager.get_sync() if run_manager else None,
            **kwargs,
        )

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        raise NotImplementedError()

    def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        raise NotImplementedError()

    @deprecated("0.1.7", alternative="invoke", removal="0.2.0")
    def __call__(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> BaseMessage:
        generation = self.generate(
            [messages], stop=stop, callbacks=callbacks, **kwargs
        ).generations[0][0]
        if isinstance(generation, ChatGeneration):
            return generation.message
        else:
            raise ValueError("Unexpected generation type")

    async def _call_async(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> BaseMessage:
        result = await self.agenerate(
            [messages], stop=stop, callbacks=callbacks, **kwargs
        )
        generation = result.generations[0][0]
        if isinstance(generation, ChatGeneration):
            return generation.message
        else:
            raise ValueError("Unexpected generation type")

    @deprecated("0.1.7", alternative="invoke", removal="0.2.0")
    def call_as_llm(
        self, message: str, stop: Optional[List[str]] = None, **kwargs: Any
    ) -> str:
        return self.predict(message, stop=stop, **kwargs)

    @deprecated("0.1.7", alternative="invoke", removal="0.2.0")
    def predict(
        self, text: str, *, stop: Optional[Sequence[str]] = None, **kwargs: Any
    ) -> str:
        if stop is None:
            _stop = None
        else:
            _stop = list(stop)
        result = self([HumanMessage(content=text)], stop=_stop, **kwargs)
        if isinstance(result.content, str):
            return result.content
        else:
            raise ValueError("Cannot use predict when output is not a string.")

    @deprecated("0.1.7", alternative="invoke", removal="0.2.0")
    def predict_messages(
        self,
        messages: List[BaseMessage],
        *,
        stop: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> BaseMessage:
        if stop is None:
            _stop = None
        else:
            _stop = list(stop)
        return self(messages, stop=_stop, **kwargs)

    @deprecated("0.1.7", alternative="ainvoke", removal="0.2.0")
    async def apredict(
        self, text: str, *, stop: Optional[Sequence[str]] = None, **kwargs: Any
    ) -> str:
        if stop is None:
            _stop = None
        else:
            _stop = list(stop)
        result = await self._call_async(
            [HumanMessage(content=text)], stop=_stop, **kwargs
        )
        if isinstance(result.content, str):
            return result.content
        else:
            raise ValueError("Cannot use predict when output is not a string.")

    @deprecated("0.1.7", alternative="ainvoke", removal="0.2.0")
    async def apredict_messages(
        self,
        messages: List[BaseMessage],
        *,
        stop: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> BaseMessage:
        if stop is None:
            _stop = None
        else:
            _stop = list(stop)
        return await self._call_async(messages, stop=_stop, **kwargs)

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get the identifying parameters."""
        return {}

    @property
    @abstractmethod
    def _llm_type(self) -> str:
        """Return type of chat model."""

    def dict(self, **kwargs: Any) -> Dict:
        """Return a dictionary of the LLM."""
        starter_dict = dict(self._identifying_params)
        starter_dict["_type"] = self._llm_type
        return starter_dict


class SimpleChatModel(BaseChatModel):
    """A simplified implementation for a chat model to inherit from."""

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        output_str = self._call(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = AIMessage(content=output_str)
        generation = ChatGeneration(message=message)
        return ChatResult(generations=[generation])

    @abstractmethod
    def _call(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        """Simpler interface."""

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return await run_in_executor(
            None,
            self._generate,
            messages,
            stop=stop,
            run_manager=run_manager.get_sync() if run_manager else None,
            **kwargs,
        )
