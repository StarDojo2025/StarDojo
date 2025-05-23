import json
import os
from typing import Dict, Any, List
import time
import asyncio
import re
from stardojo.config import Config
from stardojo.log import Logger
from stardojo.planner.base import BasePlanner
from stardojo.utils.check import check_planner_params
from stardojo.utils.file_utils import assemble_project_path, read_resource_file
from stardojo.utils.json_utils import load_json, parse_semi_formatted_text, JsonFrameStructure
from stardojo.utils.template_matching import match_templates_images, selection_box_identifier
from stardojo import constants

config = Config()
logger = Logger()

PROMPT_EXT = ".prompt"
JSON_EXT = ".json"


async def gather_information_get_completion_parallel(llm_provider, text_input_map, current_frame_path, time_stamp,
                                                     text_input, get_text_template, i,video_prefix,gathered_information_JSON):

    logger.write(f"Start gathering text information from the {i + 1}th frame")

    text_input = text_input_map if text_input is None else text_input
    image_introduction = text_input["image_introduction"]

    # Set the last frame path as the current frame path
    image_introduction[-1] = {
        "introduction": image_introduction[-1]["introduction"],
        "path": f"{current_frame_path}",
        "assistant": image_introduction[-1]["assistant"]
    }
    text_input["image_introduction"] = image_introduction
    message_prompts = llm_provider.assemble_prompt(template_str=get_text_template, params=text_input)

    logger.debug(f'{logger.UPSTREAM_MASK}{json.dumps(message_prompts, ensure_ascii=False)}\n')

    success_flag = False
    while not success_flag:
        try:
            response, info = await llm_provider.create_completion_async(message_prompts)
            logger.debug(f'{logger.DOWNSTREAM_MASK}{response}\n')

            # Convert the response to dict
            processed_response = parse_semi_formatted_text(response)
            success_flag = True
        except Exception as e:
            logger.error(f"Response is not in the correct format: {e}, retrying...")
            success_flag = False

            # wait 2 seconds for the next request and retry
            await asyncio.sleep(2)

    # Convert the response to dict
    if processed_response is None or len(response) == 0:
        logger.warn('Empty response in gather text information call')
        logger.debug("response", response, "processed_response", processed_response)

    objects = processed_response
    objects_index = str(video_prefix) + '_' + str(time_stamp)
    gathered_information_JSON.add_instance(objects_index, objects)
    logger.write(f"Finish gathering text information from the {i + 1}th frame")

    return True


def gather_information_get_completion_sequence(llm_provider, text_input_map, current_frame_path, time_stamp,
                                               text_input, get_text_template, i, video_prefix, gathered_information_JSON):

    logger.write(f"Start gathering text information from the {i + 1}th frame")
    text_input = text_input_map if text_input is None else text_input

    image_introduction = text_input["image_introduction"]

    # Set the last frame path as the current frame path
    image_introduction[-1] = {
        "introduction": image_introduction[-1]["introduction"],
        "path": f"{current_frame_path}",
        "assistant": image_introduction[-1]["assistant"]
    }
    text_input["image_introduction"] = image_introduction

    message_prompts = llm_provider.assemble_prompt(template_str=get_text_template, params=text_input)

    logger.debug(f'{logger.UPSTREAM_MASK}{json.dumps(message_prompts, ensure_ascii=False)}\n')

    response, info = llm_provider.create_completion(message_prompts)

    logger.debug(f'{logger.DOWNSTREAM_MASK}{response}\n')
    success_flag = False
    while not success_flag:
        try:
            # Convert the response to dict
            processed_response = parse_semi_formatted_text(response)
            success_flag = True
        except Exception as e:
            logger.error(f"Response is not in the correct format: {e}, retrying...")
            success_flag = False

            time.sleep(2)

    # Convert the response to dict
    if processed_response is None or len(response) == 0:
        logger.warn('Empty response in gather text information call')
        logger.debug("response", response, "processed_response", processed_response)

    objects = processed_response
    objects_index = str(video_prefix) + '_' + time_stamp
    gathered_information_JSON.add_instance(objects_index, objects)

    logger.write(f"Finish gathering text information from the {i + 1}th frame")

    return True


async def get_completion_in_parallel(llm_provider, text_input_map, extracted_frame_paths, text_input,get_text_template,video_prefix,gathered_information_JSON):
    tasks =[]

    for i, (current_frame_path, time_stamp) in enumerate(extracted_frame_paths):

        task=gather_information_get_completion_parallel(llm_provider, text_input_map, current_frame_path, time_stamp,
                                                   text_input, get_text_template, i,video_prefix,gathered_information_JSON)

        tasks.append(task)

        # wait 2 seconds for the next request
        time.sleep(2)

    return await asyncio.gather(*tasks)


async def get_completion_in_parallel_tool(
        llm_provider,
        text_input_map,
        extracted_frame_paths,
        inventory_names,
        text_input,
        get_text_template,
        video_prefix,
        gathered_information_JSON,
):
    tasks = []

    for i, (current_frame_path) in enumerate(extracted_frame_paths):
        inventory_name = inventory_names[i]

        text_input["image_introduction"][0]["inventory_name"] = inventory_name
        task = gather_information_get_completion_parallel(
            llm_provider,
            text_input_map,
            current_frame_path,
            i,
            text_input,
            get_text_template,
            i,
            video_prefix,
            gathered_information_JSON,
        )

        tasks.append(task)

        # wait 2 seconds for the next request
        time.sleep(2)

    return await asyncio.gather(*tasks)

def get_completion_in_sequence(llm_provider, text_input_map, extracted_frame_paths, text_input, get_text_template,
                               video_prefix, gathered_information_JSON):
    for i, (current_frame_path, time_stamp) in enumerate(extracted_frame_paths):
        gather_information_get_completion_sequence(llm_provider, text_input_map, current_frame_path, time_stamp,
                                                   text_input, get_text_template, i,video_prefix,gathered_information_JSON)

    return True


class InformationGathering():

    inventory_path = assemble_project_path("./res/stardew/icons/inventory")
    icon_list = []
    for f in os.listdir(inventory_path):
        icon_list.append(os.path.join(inventory_path, f))
    STARDEW_ORIGINAL_ICON_LIST = icon_list

    def __init__(
            self,
            input_map: Dict = None,
            template: str = None,
            icon_replacer: Any = None,
            object_detector: Any = None,
            llm_provider: Any = None,
            text_input_map: Dict = None,
            get_text_template: str = None,
            toolbar_input_map: Dict = None,
            get_toolbar_template: str = None,
            frame_extractor: Any = None,
    ):

        self.input_map = input_map
        self.template = template
        self.icon_replacer = icon_replacer
        self.object_detector = object_detector
        self.llm_provider = llm_provider
        self.text_input_map = text_input_map
        self.get_text_template = get_text_template
        self.toolbar_input_map = toolbar_input_map
        self.get_toolbar_template = get_toolbar_template
        self.frame_extractor = frame_extractor


    def _pre(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return input


    def __call__(self, *args, input: Dict[str, Any] = None, class_=None, **kwargs) -> Dict[str, Any]:
        gather_information_configurations = input["gather_information_configurations"]
        cur_inventories_shot_paths = input["cur_inventories_shot_paths"]
        cur_new_icon_image_shot_path = input["cur_new_icon_image_shot_path"]
        cur_new_icon_name_image_shot_path = input["cur_new_icon_name_image_shot_path"]

        frame_extractor_gathered_information = None
        icon_replacer_gathered_information = None
        object_detector_gathered_information = None
        llm_description_gathered_information = None

        input = self.input_map if input is None else input
        input = self._pre(input=input)

        image_files = []
        if "image_introduction" in input.keys():
            for image_info in input["image_introduction"]:
                image_files.append(image_info["path"])

        # flag = True
        processed_response = {}

        # Gather information by frame extractor
        if gather_information_configurations[constants.FRAME_EXTRACTOR] is True:

            logger.write(f"Using frame extractor to gather information")

            if self.frame_extractor is not None:

                text_input = input["text_input"]
                video_path = input["video_clip_path"]

                if "test_text_image" in input.keys() and input["test_text_image"]:  # offline test
                    extracted_frame_paths = input["test_text_image"]

                else:  # online run
                    # extract the text information of the whole video
                    # run the frame_extractor to get the key frames
                    extracted_frame_paths = self.frame_extractor.extract(video_path=video_path)

                # Gather information by Icon replacer
                if gather_information_configurations["icon_replacer"] is True:
                    logger.write(f"Using icon replacer to gather information")
                    if self.icon_replacer is not None:
                        try:
                            extracted_frame_paths = self._replace_icon(extracted_frame_paths)
                        except Exception as e:
                            logger.error(f"Error in gather information by Icon replacer: {e}")
                            flag = False
                    else:
                        logger.warn('Icon replacer is not set, skipping gather information by Icon replacer')

                # For each keyframe, use llm to get the text information
                video_prefix = os.path.basename(video_path).split('.')[0].split('_')[-1]  # Different video should have differen prefix for avoiding the same time stamp
                frame_extractor_gathered_information = JsonFrameStructure()

                if config.parallel_request_gather_information:
                    # Create completions in parallel
                    logger.write(f"Start gathering text information from the whole video in parallel")

                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    try:
                        loop.run_until_complete(
                            get_completion_in_parallel(self.llm_provider, self.text_input_map, extracted_frame_paths,
                                                       text_input,self.get_text_template,video_prefix,frame_extractor_gathered_information))

                    except KeyboardInterrupt:

                        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                        for task in tasks:
                            task.cancel()

                        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                    finally:
                        loop.close()

                else:
                    logger.write(f"Start gathering text information from the whole video in sequence")
                    get_completion_in_sequence(self.llm_provider, self.text_input_map, extracted_frame_paths,
                                               text_input,self.get_text_template,video_prefix,frame_extractor_gathered_information)

                frame_extractor_gathered_information.sort_index_by_timestamp()
                logger.write(f"Finish gathering text information from the whole video")

            else:
                logger.warn('Frame extractor is not set, skipping gather information by frame extractor')
                frame_extractor_gathered_information = None

            # Get dialogue information from the gathered_information_JSON at the subfounder find the dialogue frames
            if frame_extractor_gathered_information is not None:
                dialogues = [item["values"] for item in frame_extractor_gathered_information.search_type_across_all_indices("dialogue")]
            else:
                if self.frame_extractor is not None:
                    msg = "No gathered_information_JSON received, so no dialogue information is provided."
                else:
                    msg = "No gathered_information_JSON available, no Frame Extractor in use."

                logger.warn(msg)
                dialogues = []

            # Update the <$task_description$> in the gather_information template with the latest task_description
            all_task_guidance = frame_extractor_gathered_information.search_type_across_all_indices(constants.TASK_GUIDANCE)

            # Remove the content of "task is none"
            all_task_guidance = [task_guidance for task_guidance in all_task_guidance if constants.NONE_TASK_OUTPUT not in task_guidance["values"].lower()]

            if len(all_task_guidance) != 0:
                # New task guidance is found, use the latest one
                last_task_guidance = max(all_task_guidance, key=lambda x: x['index'])['values']
                input[constants.TASK_DESCRIPTION] = last_task_guidance  # this is for the input of the gather_information

            # @TODO: summary the dialogue and use it

        # Gather information of the toolbar
        # 1.identify new item in the toolbar
        # TODO: identify new item in the toolbar (still not complete)
        # new_icon_template_list = self.gather_information_of_new_icon(cur_new_icon_image_shot_path,cur_new_icon_name_image_shot_path)
        new_icon_template_list = []

        # run gather toolbar info and llm_description in parallel
        results = asyncio.run(self.execute_parallel(cur_inventories_shot_paths, gather_information_configurations, input))
        toolbar_dict_list, selected_position, processed_response, flag = results
        llm_description_gathered_information=processed_response

        # Assemble the gathered_information_JSON

        if flag:
            objects = []

            if icon_replacer_gathered_information is not None and "objects" in icon_replacer_gathered_information:
                objects.extend(icon_replacer_gathered_information["objects"])
            if object_detector_gathered_information is not None and "objects" in object_detector_gathered_information:
                objects.extend(object_detector_gathered_information["objects"])
            if llm_description_gathered_information is not None and "objects" in llm_description_gathered_information:
                objects.extend(llm_description_gathered_information["objects"])

            objects = list(set(objects))

            processed_response["objects"] = objects
            processed_response['toolbar_dict_list'] = toolbar_dict_list
            processed_response['selected_position'] = selected_position

            # Merge the gathered_information_JSON to the processed_response
            processed_response["gathered_information_JSON"] = frame_extractor_gathered_information

            if gather_information_configurations[constants.FRAME_EXTRACTOR] is True:
                if len(all_task_guidance) == 0:
                    processed_response[constants.LAST_TASK_GUIDANCE] = ""
                else:
                    processed_response[constants.LAST_TASK_GUIDANCE] = last_task_guidance

        # Gather information by object detector, which is grounding dino.
        if gather_information_configurations[constants.OBJECT_DETECTOR] is True:
            logger.write(f"Using object detector to gather information")
            if self.object_detector is not None:
                try:
                    target_object_name = processed_response[constants.TARGET_OBJECT_NAME].lower() \
                        if constants.NONE_TARGET_OBJECT_OUTPUT not in processed_response[constants.TARGET_OBJECT_NAME].lower() else ""

                    image_source, boxes, logits, phrases = self.object_detector.detect(image_path=image_files[0],
                                                                                       text_prompt= target_object_name,
                                                                                       box_threshold=0.4, device='cuda')
                    processed_response["boxes"] = boxes
                    processed_response["logits"] = logits
                    processed_response["phrases"] = phrases
                except Exception as e:
                    logger.error(f"Error in gather information by object detector: {e}")
                    flag = False

                try:
                    minimap_detection_objects = self.object_detector.process_minimap_targets(image_files[0])

                    processed_response.update({constants.MINIMAP_INFORMATION:minimap_detection_objects})

                except Exception as e:
                    logger.error(f"Error in gather information by object detector for minimap: {e}")
                    flag = False

        success = self._check_success(data=processed_response)

        data = dict(
            flag=flag,
            success=success,
            input=input,
            res_dict=processed_response,
        )

        data = self._post(data=data)

        return data

    def _post(self, *args, data: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return data


    def _check_success(self, *args, data, **kwargs):

        success = False

        prop_name = "description"

        if prop_name in data.keys():
            desc = data[prop_name]
            success = desc is not None and len(desc) > 0
        return success


    def _replace_icon(self, extracted_frame_paths):
        extracted_frames = [frame[0] for frame in extracted_frame_paths]
        extracted_timesteps = [frame[1] for frame in extracted_frame_paths]
        extracted_frames = self.icon_replacer(image_paths=extracted_frames)
        extracted_frame_paths = list(zip(extracted_frames, extracted_timesteps))
        return extracted_frame_paths


    def gather_information_of_new_icon(self, cur_new_icon_image_shot_path, cur_new_icon_name_image_shot_path):
        # if there is a new icon in the screenshot, save it to the workdir for later template matching

        # request the llm to decide if there is a new icon and get the name of the new icon

        # if there is a new icon, rename it with the name in LLM response

        # if LLM response is empty, delete the new icon images

        # return the list of icon paths

        pass

    async def template_matching_for_current_toolbar(self, sr_file_list, base_template_file_list, work_template_file_list):
        matching_dict = match_templates_images(sr_file_list, base_template_file_list, work_template_file_list)
        selected_position=None
        for sr_file in sr_file_list:
            is_selected=selection_box_identifier(sr_file, config.selection_box_region)
            if is_selected:
                selected_position=sr_file_list.index(sr_file)+1
                break
        for key in matching_dict:
            matching_dict[key] = os.path.splitext(os.path.basename(matching_dict[key]))[0]
        return matching_dict,selected_position

    async def gather_toolbar_list(self, match_dict, get_number_flag=True):
        any_key = next(iter(match_dict.keys()))
        video_prefix = any_key.split("/")[-2]
        frame_paths = []
        for path in match_dict:
            frame_paths.append(path)
        names = []
        for path in match_dict:
            names.append(match_dict[path])

        frame_extractor_gathered_information = JsonFrameStructure()
        text_input = self.toolbar_input_map

        if get_number_flag:
            # Create completions in parallel
            logger.write(
                f"Start gathering text information from the whole video in parallel"
            )

            await get_completion_in_parallel_tool(
                self.llm_provider,
                self.toolbar_input_map,
                frame_paths,
                names,
                text_input,
                self.get_toolbar_template,
                video_prefix,
                frame_extractor_gathered_information,
            )

            inventory_index_list = []
            item_number_list = []
            for key_1 in frame_extractor_gathered_information.data_structure:
                for key_2 in frame_extractor_gathered_information.data_structure[key_1]:
                    pattern = r"_([0-9]+)$"
                    match = re.search(pattern, key_2)
                    inventory_index = match.group(1)
                    contents = frame_extractor_gathered_information.data_structure[key_1][key_2]
                    item_number = self.extract_number(contents)
                    inventory_index_list.append(inventory_index)
                    item_number_list.append(item_number)
        else:
            # creat a item_number_list will all 1
            item_number_list = [1] * len(names)
            inventory_index_list = [str(i) for i in range(len(names))]

        toolbar_dict_list = []
        for i in range(len(inventory_index_list)):

            name = names[i]
            number = item_number_list[inventory_index_list.index(str(i))]
            position = i + 1

            toolbar_dict_list.append({
                "name": name,
                "number": number,
                "position": position
            })

        return toolbar_dict_list

    def extract_number(self, data):
        for item in data:
            if None in item:
                value = item[None]  # 'Number: 1'
                parts = value.split(': ')  # ['Number', '1']
                if len(parts) == 2:
                    try:
                        return int(parts[1])
                    except ValueError:
                        print("Cannot convert to integer.")
                        return None

    async def gather_toolbar_parallel(self, cur_inventories_shot_paths, gather_information_configurations):
        new_icon_template_list = []
        match_dict, selected_position = await self.template_matching_for_current_toolbar(
            cur_inventories_shot_paths, self.STARDEW_ORIGINAL_ICON_LIST, new_icon_template_list
        )
        toolbar_dict_list = await self.gather_toolbar_list(
            match_dict, get_number_flag=gather_information_configurations[constants.GET_ITEM_NUMBER]
        )
        return toolbar_dict_list,selected_position

    async def gather_llm_description(self, input):
        flag=True
        gather_information_configurations = input["gather_information_configurations"]
        if gather_information_configurations[constants.LLM_DESCRIPTION] is True:
            logger.write(f"Using llm description to gather information")
            try:
                # Call the LLM provider for gather information json
                message_prompts = self.llm_provider.assemble_prompt(template_str=self.template, params=input)

                logger.debug(f'{logger.UPSTREAM_MASK}{json.dumps(message_prompts, ensure_ascii=False)}\n')

                gather_information_success_flag = False
                while gather_information_success_flag is False:
                    try:
                        response, info = self.llm_provider.create_completion(message_prompts)
                        logger.debug(f'{logger.DOWNSTREAM_MASK}{response}\n')

                        # Convert the response to dict
                        processed_response = parse_semi_formatted_text(response)
                        gather_information_success_flag = True

                    except Exception as e:
                        logger.error(f"Response of image description is not in the correct format: {e}, retrying...")
                        gather_information_success_flag = False

                        # Wait 2 seconds for the next request and retry
                        time.sleep(2)

                llm_description_gathered_information = processed_response

            except Exception as e:
                logger.error(f"Error in gather image description information: {e}")
                flag = False
            return llm_description_gathered_information,flag
        else:
            return None,False


    async def execute_parallel(self, cur_inventories_shot_paths, gather_information_configurations,
                               input):
        # try:
            task_a = self.gather_toolbar_parallel(cur_inventories_shot_paths, gather_information_configurations)
            task_b = self.gather_llm_description(input)
            tool_bar_results, llm_results = await asyncio.gather(task_a, task_b)

            toolbar_dict_list, selected_position = tool_bar_results
            processed_response, flag = llm_results
            llm_description_gathered_information = processed_response
            # Handle results here or return them
            return toolbar_dict_list, selected_position, llm_description_gathered_information, flag

        # except asyncio.CancelledError:
        #     # Handle task cancellation here
        #     print("Tasks were cancelled")
        #     return None, None, None, False
        # except Exception as e:
        #     print(f"An error occurred: {e}")
        #     return None, None, None, False



class ActionPlanning():
    def __init__(self,
                 input_map: Dict = None,
                 template: Dict = None,
                 llm_provider: Any = None,
                 ):

        self.input_map = input_map
        self.template = template
        self.llm_provider = llm_provider


    def _pre(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return input


    def __call__(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:

        input = self.input_map if input is None else input
        input = self._pre(input=input)

        flag = True
        processed_response = {}

        try:
            message_prompts = self.llm_provider.assemble_prompt(template_str=self.template, params=input)

            logger.debug(f'{logger.UPSTREAM_MASK}{json.dumps(message_prompts, ensure_ascii=False)}\n')

            # Call the LLM provider for decision making
            response, info = self.llm_provider.create_completion(message_prompts)

            logger.debug(f'{logger.DOWNSTREAM_MASK}{response}\n')

            if response is None or len(response) == 0:
                logger.warn('No response in decision making call')
                logger.debug(input)

            # Convert the response to dict
            processed_response = parse_semi_formatted_text(response)

        except Exception as e:
            logger.error(f"Error in decision_making: {e}")
            logger.error_ex(e)
            flag = False

        data = dict(
            flag=flag,
            input=input,
            res_dict=processed_response,
        )

        data = self._post(data=data)
        return data


    def _post(self, *args, data: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return data


class SuccessDetection():
    def __init__(self,
                 input_map: Dict = None,
                 template: Dict = None,
                 llm_provider: Any = None,
                 ):
        self.input_map = input_map
        self.template = template
        self.llm_provider = llm_provider


    def _pre(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return input


    def __call__(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:

        input = self.input_map if input is None else input
        input = self._pre(input=input)

        flag = True
        processed_response = {}

        try:

            # Call the LLM provider for success detection
            message_prompts = self.llm_provider.assemble_prompt(template_str=self.template, params=input)

            logger.debug(f'{logger.UPSTREAM_MASK}{json.dumps(message_prompts, ensure_ascii=False)}\n')

            response, info = self.llm_provider.create_completion(message_prompts)

            logger.debug(f'{logger.DOWNSTREAM_MASK}{response}\n')

            # Convert the response to dict
            processed_response = parse_semi_formatted_text(response)

        except Exception as e:
            logger.error(f"Error in success_detection: {e}")
            flag = False

        data = dict(
            flag=flag,
            input=input,
            res_dict=processed_response,
        )

        data = self._post(data=data)
        return data


    def _post(self, *args, data: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return data


class SelfReflection():

    def __init__(self,
                 input_map: Dict = None,
                 template: Dict = None,
                 llm_provider: Any = None,
                 ):
        self.input_map = input_map
        self.template = template
        self.llm_provider = llm_provider


    def _pre(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return input


    def __call__(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:

        input = self.input_map if input is None else input
        input = self._pre(input=input)

        flag = True
        processed_response = {}

        try:

            # Call the LLM provider for self reflection
            message_prompts = self.llm_provider.assemble_prompt(template_str=self.template, params=input)

            logger.debug(f'{logger.UPSTREAM_MASK}{json.dumps(message_prompts, ensure_ascii=False)}\n')

            response, info = self.llm_provider.create_completion(message_prompts)

            logger.debug(f'{logger.DOWNSTREAM_MASK}{response}\n')

            # Convert the response to dict
            processed_response = parse_semi_formatted_text(response)

        except Exception as e:
            logger.error(f"Error in self reflection: {e}")
            flag = False

        data = dict(
            flag=flag,
            input=input,
            res_dict=processed_response,
        )

        data = self._post(data=data)
        return data


    def _post(self, *args, data: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return data


class TaskInference():

    def __init__(self,
                 input_map: Dict = None,
                 template: Dict = None,
                 llm_provider: Any = None,
                 ):

        self.input_map = input_map
        self.template = template
        self.llm_provider = llm_provider


    def _pre(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return input


    def __call__(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:

        input = self.input_map if input is None else input
        input = self._pre(input=input)

        flag = True
        processed_response = {}
        res_json = None

        try:

            # Call the LLM provider for information summary
            message_prompts = self.llm_provider.assemble_prompt(template_str=self.template, params=input)

            logger.debug(f'{logger.UPSTREAM_MASK}{json.dumps(message_prompts, ensure_ascii=False)}\n')

            response, info = self.llm_provider.create_completion(message_prompts)

            logger.debug(f'{logger.DOWNSTREAM_MASK}{response}\n')

            # Convert the response to dict
            processed_response = parse_semi_formatted_text(response)

        except Exception as e:
            logger.error(f"Error in information_summary: {e}")
            flag = False

        data = dict(
            flag=flag,
            input=input,
            res_dict=processed_response,
            # res_json=res_json,
        )

        data = self._post(data=data)
        return data


    def _post(self, *args, data: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        return data

class StardewPlanner(BasePlanner):

    def __init__(self,
                 llm_provider: Any = None,
                 planner_params: Dict = None,
                 use_task_inference: bool = False,
                 use_self_reflection: bool = False,
                 gather_information_max_steps: int = 1,  # 5,
                 icon_replacer: Any = None,
                 object_detector: Any = None,
                 frame_extractor: Any = None,
                 ):
        """
        inputs: input key-value pairs
        templates: template for composing the prompt
        """

        super(BasePlanner, self).__init__()

        self.llm_provider = llm_provider

        self.use_task_inference = use_task_inference
        self.use_self_reflection = use_self_reflection
        self.gather_information_max_steps = gather_information_max_steps

        self.icon_replacer = icon_replacer
        self.object_detector = object_detector
        self.frame_extractor = frame_extractor
        self.set_internal_params(planner_params=planner_params,
                                 use_task_inference=use_task_inference)


    # Allow re-configuring planner
    def set_internal_params(self,
                            planner_params: Dict = None,
                            use_task_inference: bool = False):

        self.planner_params = planner_params
        if not check_planner_params(self.planner_params):
            raise ValueError(f"Error in planner_params: {self.planner_params}")

        self.inputs = self._init_inputs()
        self.templates = self._init_templates()

        self.information_gathering_ = InformationGathering(input_map=self.inputs["information_gathering"],
                                                     template=self.templates["information_gathering"],
                                                     text_input_map=self.inputs["information_text_gathering"],
                                                     get_text_template=self.templates["information_text_gathering"],
                                                     toolbar_input_map=self.inputs["information_toolbar_gathering"],
                                                     get_toolbar_template=self.templates["information_toolbar_gathering"],
                                                     frame_extractor=self.frame_extractor,
                                                     icon_replacer=self.icon_replacer,
                                                     object_detector=self.object_detector,
                                                     llm_provider=self.llm_provider)

        self.action_planning_ = ActionPlanning(input_map=self.inputs["action_planning"],
                                               template=self.templates["action_planning"],
                                               llm_provider=self.llm_provider)

        self.success_detection_ = SuccessDetection(input_map=self.inputs["success_detection"],
                                                   template=self.templates["success_detection"],
                                                   llm_provider=self.llm_provider)

        if self.use_self_reflection:
            self.self_reflection_ = SelfReflection(input_map=self.inputs["self_reflection"],
                                                   template=self.templates["self_reflection"],
                                                   llm_provider=self.llm_provider)
        else:
            self.self_reflection_ = None

        if self.use_task_inference:
            self.task_inference_ = TaskInference(input_map=self.inputs["task_inference"],
                                                           template=self.templates["task_inference"],
                                                           llm_provider=self.llm_provider)
        else:
            self.task_inference_ = None


    def _init_inputs(self):

        input_examples = dict()
        prompt_paths = self.planner_params["prompt_paths"]
        input_example_paths = prompt_paths["inputs"]

        for key, value in input_example_paths.items():
            path = assemble_project_path(value)
            if path.endswith(PROMPT_EXT):
                input_examples[key] = read_resource_file(path)
            else:
                input_examples[key] = load_json(path)

        return input_examples


    def _init_templates(self):

        templates = dict()
        prompt_paths = self.planner_params["prompt_paths"]
        template_paths = prompt_paths["templates"]

        for key, value in template_paths.items():
            path = assemble_project_path(value)
            if path.endswith(PROMPT_EXT):
                templates[key] = read_resource_file(path)
            else:
                templates[key] = load_json(path)

        return templates


    def information_gathering(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:

        if input is None:
            input = self.inputs["gather_information"]

        image_file = input["image_introduction"][0]["path"]

        for i in range(self.gather_information_max_steps):
            data = self.information_gathering_(input=input, class_=None)

            success = data["success"]

            if success:
                break

        return data


    def action_planning(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:

        if input is None:
            input = self.inputs["action_planning"]

        data = self.action_planning_(input=input)

        return data


    def success_detection(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:

        if input is None:
            input = self.inputs["success_detection"]

        data = self.success_detection_(input=input)

        return data


    def self_reflection(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:

        if input is None:
            input = self.inputs["self_reflection"]

        data = self.self_reflection_(input=input)

        return data


    def task_inference(self, *args, input: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:

        if input is None:
            input = self.inputs["task_inference"]

        data = self.task_inference_(input=input)

        return data