import argparse
import copy
import json
import secrets
from pathlib import Path

from .config import POSE_TEMPLATE_PATH, TEMPLATE_PATH


MODEL_IDS = ("451", "453", "454", "469")
NODE_TYPES = {
    "451": "UNETLoader", "453": "CLIPLoader", "454": "VAELoader",
    "469": "QwenImage21Cache", "474": "TextEncodeQwenImage21",
    "458": "KSampler", "457": "VAEDecode", "503": "SaveImageWebsocket",
}
POSE_MODEL_IDS = ("451", "453", "454", "pose_lora", "pose_cache")
POSE_NODE_TYPES = {key: value for key, value in NODE_TYPES.items() if key != "469"} | {
    "pose_lora": "LoraLoaderModelOnly", "pose_cache": "QwenImage21Cache", "pose_latent": "EmptyLatentImage",
}
POSE_PROMPT = "Draw character from image2\n"


class Workflow:
    def __init__(self, path=TEMPLATE_PATH):
        self.template = json.loads(Path(path).read_text())
        if {key: node["class_type"] for key, node in self.template.items()} != NODE_TYPES:
            raise ValueError("Unexpected workflow nodes; convert the supplied Qwen workflow first")
        self.pose_template = json.loads(POSE_TEMPLATE_PATH.read_text())
        if {key: node["class_type"] for key, node in self.pose_template.items()} != POSE_NODE_TYPES:
            raise ValueError("Unexpected pose workflow nodes")
        self.pose_used = False
        self.build(["startup-validation"], "startup-validation")

    def build(self, image_keys, prompt):
        return self._build(self.template, image_keys, prompt)

    def build_pose(self, image_keys, prompt, width, height):
        graph = self._build(self.pose_template, image_keys, POSE_PROMPT + prompt)
        graph["pose_latent"]["inputs"].update(width=width, height=height)
        self.pose_used = True
        return graph

    def _build(self, template, image_keys, prompt):
        if not 1 <= len(image_keys) <= 10:
            raise ValueError("Expected 1–10 retained images")
        graph = copy.deepcopy(template)
        inputs = graph["474"]["inputs"]
        inputs["prompt"] = prompt
        for index, key in enumerate(image_keys, 1):
            node_id = f"memory_{index}"
            graph[node_id] = {"class_type": "MemoryImageInput", "inputs": {"image_key": key}}
            inputs[f"images.image_{index}"] = [node_id, 0]
        graph["458"]["inputs"]["seed"] = secrets.randbits(64)
        self.validate_links(graph)
        return graph

    def cleanup(self, task_id):
        graph = {key: copy.deepcopy(self.template[key]) for key in MODEL_IDS}
        if self.pose_used:
            graph.update({key: copy.deepcopy(self.pose_template[key]) for key in POSE_MODEL_IDS})
        graph["memory_release"] = {"class_type": "MemoryImageRelease", "inputs": {"task_id": task_id}}
        return graph

    @staticmethod
    def validate_links(graph):
        for node in graph.values():
            for value in node["inputs"].values():
                if isinstance(value, list) and (len(value) != 2 or value[0] not in graph):
                    raise ValueError("Dangling workflow link")


def convert(source):
    root = json.loads(Path(source).read_text())
    top = next(node for node in root["nodes"] if node["id"] == 459)
    values = top["widgets_values_named"]
    if values["switch"]:
        raise ValueError("This converter expects the supplied workflow's custom_size=false")
    subgraph = next(graph for graph in root["definitions"]["subgraphs"] if graph["id"] == top["type"])
    nodes = {str(node["id"]): node for node in subgraph["nodes"]}
    graph = {key: {"class_type": kind, "inputs": {}} for key, kind in NODE_TYPES.items()}
    graph["451"]["inputs"] = {"unet_name": values["unet_name"], "weight_dtype": nodes["451"]["widgets_values_named"]["weight_dtype"]}
    graph["453"]["inputs"] = {"clip_name": values["clip_name"], "type": "qwen_image", "device": "default"}
    graph["454"]["inputs"] = {"vae_name": values["vae_name"]}
    graph["469"]["inputs"] = {"model": ["451", 0], "device": values["device"], "dtype": values["dtype"]}
    graph["474"]["inputs"] = {"clip": ["453", 0], "vae": ["454", 0], "prompt": "", "negative_prompt": values["negative_prompt"], "resolution": values["resolution"]}
    graph["458"]["inputs"] = {
        "model": ["469", 0], "positive": ["474", 0], "negative": ["474", 1], "latent_image": ["474", 2],
        "seed": 0, "steps": values["steps"], "cfg": values["cfg"], "sampler_name": values["scheduler"],
        "scheduler": values["scheduler_1"], "denoise": nodes["458"]["widgets_values_named"]["denoise"],
    }
    graph["457"]["inputs"] = {"samples": ["458", 0], "vae": ["454", 0]}
    graph["503"]["inputs"] = {"images": ["457", 0]}
    return graph


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert the supplied Qwen UI workflow; contains no uploaded image data")
    parser.add_argument("source")
    parser.add_argument("--output", type=Path, default=TEMPLATE_PATH)
    args = parser.parse_args()
    args.output.write_text(json.dumps(convert(args.source), indent=2, ensure_ascii=False) + "\n")
