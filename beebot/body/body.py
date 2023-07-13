import logging
import os.path

from autopack.pack import Pack
from langchain.chat_models import ChatOpenAI
from langchain.chat_models.base import BaseChatModel
from langchain.schema import AIMessage
from playwright.sync_api import Playwright, PlaywrightContextManager
from pydantic import ValidationError
from statemachine import StateMachine, State

from beebot.body.llm import call_llm
from beebot.body.pack_utils import all_packs, system_packs
from beebot.config import Config
from beebot.config.config import IDEAL_MODEL
from beebot.decider import Decider
from beebot.executor import Executor
from beebot.interpreter import Interpreter
from beebot.memory import Memory
from beebot.memory.memory_storage import MemoryStorage
from beebot.models import Action, Stimulus
from beebot.models.observation import Observation
from beebot.planner import Planner
from beebot.prompting.function_selection import initial_selection_template

logger = logging.getLogger(__name__)

RETRY_LIMIT = 3


class BodyStateMachine(StateMachine):
    setup = State(initial=True)
    starting = State()
    planning = State()
    deciding = State()
    executing = State()
    waiting = State()
    done = State(final=True)

    start = setup.to(starting)
    plan = starting.to(planning) | waiting.to(planning)
    decide = waiting.to(deciding)
    execute = waiting.to(executing)
    wait = (
        deciding.to(waiting)
        | planning.to(waiting)
        | executing.to(waiting)
        | starting.to(waiting)
    )
    finish = waiting.to(done) | executing.to(done)


class Body:
    initial_task: str
    current_plan: str
    state: BodyStateMachine
    packs: dict["Pack"]
    memories: MemoryStorage
    playwright: Playwright

    llm: BaseChatModel
    planner: Planner
    interpreter: Interpreter
    executor: Executor
    decider: Decider
    config: Config

    def __init__(self, initial_task: str):
        self.initial_task = initial_task
        self.current_plan = None
        self.state = BodyStateMachine()
        self.config = Config.from_env()
        self.memories = MemoryStorage()

        self.llm = ChatOpenAI(model_name=IDEAL_MODEL, model_kwargs={"top_p": 0.2})
        self.planner = Planner(body=self)
        self.decider = Decider(body=self)
        self.executor = Executor(body=self)
        self.interpreter = Interpreter(body=self)
        self.packs = {}

        if not os.path.exists(self.config.workspace_path):
            os.makedirs(self.config.workspace_path, exist_ok=True)

    def setup(self):
        """These are here instead of init because they involve network requests"""

        self.playwright = PlaywrightContextManager().start()
        self.packs = system_packs(self)
        self.update_packs()

        self.state.start()

    def plan(self):
        """Turn the initial task into a plan"""
        self.state.plan()
        try:
            self.current_plan = self.planner.plan()
        finally:
            self.state.wait()

    def cycle(self, stimulus: Stimulus = None, retry_count: int = 0) -> Memory:
        """Step through one stimulus-action-observation loop"""
        if self.state.current_state == BodyStateMachine.done:
            return

        self.plan()
        self.update_packs()

        # If a stimulus was not supplied, generate one from history
        if not stimulus:
            stimulus = Stimulus.generate_stimulus(self)

        self.memories.add_stimulus(stimulus=stimulus)

        action = self.decide_and_interpret_with_retry(stimulus)
        self.memories.add_action(action=action)

        try:
            observation = self.execute(action=action)
            self.memories.add_observation(observation)
        except ValidationError as e:
            # It's likely the AI just sent bad arguments, try again.
            logger.warning(
                f"Invalid arguments received: {e}. {action.tool_name}({action.tool_args}"
            )
            if retry_count >= RETRY_LIMIT:
                return
            return self.cycle(stimulus, retry_count + 1)

        complete_memory = self.memories.finish()
        return complete_memory

    def decide_and_interpret_with_retry(
        self, stimulus: Stimulus, retry_count: int = 0, previous_response: str = ""
    ) -> Action:
        if retry_count and previous_response:
            stimulus.input.content += (
                f"\n\nWarning: You have attempted this next action in the past unsuccessfully. Please reassess your "
                f"strategy. Your failed attempt is: {previous_response}"
            )

        brain_output = self.decide(stimulus)
        try:
            return self.interpreter.interpret_brain_output(brain_output)
        except ValueError:
            logger.warning("Got invalid response from LLM, retrying...")
            if retry_count >= RETRY_LIMIT:
                raise ValueError(f"Got invalid response {RETRY_LIMIT} times in a row")
            return self.decide_and_interpret_with_retry(
                stimulus, retry_count + 1, previous_response=brain_output.content
            )

    def execute(self, action: Action) -> Observation:
        """Execute an action and keep track of state"""
        self.state.execute()
        try:
            result = self.executor.execute(action=action)
            return result
        finally:
            # If the action resulted in status change (e.g. task complete) don't do anything
            if self.state.current_state == self.state.executing:
                self.state.wait()

    def decide(self, stimulus: Stimulus) -> AIMessage:
        """Execute an action and keep track of state"""
        self.state.decide()

        try:
            self.memories.add_stimulus(stimulus)
            return self.decider.decide(stimulus=stimulus)
        finally:
            self.state.wait()

    def recommend_packs_for_current_plan(self) -> list[dict[str, str]]:
        # TODO: This should probably be mostly, if not entirely, in Brain
        # Use the plan if we have it, otherwise just use the task.
        user_input = self.current_plan or self.initial_task
        functions_string = []
        for pack in all_packs(self).values():
            formatted_args = [
                f"{arg.get('name')}: {arg.get('type')}"
                for arg in pack.run_args.values()
            ]
            functions_string.append(f"{pack.name}({formatted_args})")

        prompt = initial_selection_template().format(
            user_input=user_input, functions_string="\n".join(functions_string)
        )

        response = call_llm(self, [prompt])

        return [p.strip() for p in response.content.split(",")]

    def update_packs(self) -> list[Pack]:
        packs = all_packs(self)
        for pack_name in self.recommend_packs_for_current_plan():
            try:
                pack = packs[pack_name]
                pack.init_tool()
                self.packs[pack_name] = pack
            except Exception as e:
                logger.warning(f"Pack {pack_name} could not be initialized: {e}")
