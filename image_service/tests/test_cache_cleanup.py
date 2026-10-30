import gc
import weakref

import pytest
import torch

import nodes
from comfy_execution.caching import CacheKeySetInputSignature, HierarchicalCache
from comfy_execution.graph import DynamicPrompt
from execution import CacheEntry
from image_service.workflow import Workflow


class Unchanged:
    async def get(self, node_id):
        return False


class StubNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}


@pytest.mark.asyncio
async def test_native_classic_cache_prunes_images_and_preserves_model_objects(monkeypatch):
    workflow = Workflow()
    graph = workflow.build(["unique-key"], "edit")
    cleanup = workflow.cleanup("test-task")
    for node in [*graph.values(), *cleanup.values()]:
        monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS, node["class_type"], StubNode)
    cache = HierarchicalCache(CacheKeySetInputSignature)
    await cache.set_prompt(DynamicPrompt(graph), graph.keys(), Unchanged())
    model = object()
    await cache.set("451", CacheEntry({}, [model]))
    input_image = torch.empty(1, 32, 32, 3)
    output_image = torch.empty(1, 32, 32, 3)
    input_ref, output_ref = weakref.ref(input_image), weakref.ref(output_image)
    await cache.set("memory_1", CacheEntry({}, [input_image]))
    await cache.set("457", CacheEntry({}, [output_image]))
    del input_image, output_image
    assert input_ref() is not None and output_ref() is not None
    await cache.set_prompt(DynamicPrompt(cleanup), cleanup.keys(), Unchanged())
    cache.clean_unused()
    gc.collect()
    assert input_ref() is None and output_ref() is None
    assert (await cache.get("451")).outputs[0] is model
