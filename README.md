## PatrickStar: Parallel Training of Large Language Models via a Chunk-based Memory Management

![logo](./logo.png)

### Meeting PatrickStar
Pre-Trained Models (PTM) are becoming the hotspot of both NLP research and industry application. They are models that are trained with massive data and have learned generic features of the language. In pactice, they are fine-tuned for downstream tasks with task-specific datasets. In this way, PTMs have achieved great performances in almost every tasks. However, the training of PTMs requires enormous hardware resources, which makes it only accessible to small portion of people in the AI community. Now, **PatrickStar will make PTM training available to everyone!**

Out of memory error (OOM) is the nightmare of every engineer training PTMs. To prevent such error, we often have to introduce more GPUs to store the model params. PatrickStar brings a better solution for such problem. With the heterogeneous training (DeepSpeed Zero Stage 3 also uses it), PatrickStar could make full use of both the CPU and GPU memory, so that you could use fewer GPUs to train larger models.

We noticed that the GPU memory usage varies during training, but the current heterogenous training solutions are all statically spliting the model and optimizer states to CPU and GPU. To make better use of the GPU, PatrickStar proposes a dynamic memory scheduling with the help of a chunk-based memory management module. The memory management of PatrickStar supports offloading everything but the current computing part of the model to CPU. This results in training a much larger model within the same hardware environment. In terms of performance, the chunk-based memory management takes advantage of the linear structure of the transformer-based PTMs, so that it will inherently prefetch the upcoming layers to GPUs, resulting in a great performance.

In experiment, Patrickstar v0.3.0 is able to train a 12B param model with 8 Tesla V100 GPU and 240GB GPU memory, which is twice as large as the state of art. And the performance of PatrickStar is better for models of the same size as well. The deeps indicates performance of DeepSpeed v0.4.3 using the official example [DeepSpeed example](https://github.com/microsoft/DeepSpeedExamples/blob/master/Megatron-LM-v1.1.5-ZeRO3/examples/ds_pretrain_gpt2-zero3.sh) zero3 stage with activation optimzations openinig by default.

![alt perf](./doc/mgpu_scalability.png "performance testing result")

We also evaluated PatrickStar v0.4.3 on a node of 8xA100 SuperPod. It is able to train 40B model on 8xA100 with 1TB CPU memory, which is 4x larger than DeepSpeed v0.5.7. Besides the model scale, PatrickStar is way more efficient than DeepSpeed, which makes us unbelievable, and we have to check it with DeepSpeed Team before presenting the DeepSpeed results. The benchmark scripts are in [./examples/benchmark](here).
![alt perf](./doc/mgpu_perf_a100.png "performance testing result on SuperNode")


We've also trained the [CLUE-GPT2](https://huggingface.co/uer/gpt2-chinese-cluecorpussmall) model with PatrickStar, the loss and accuracy curve is shown below:

![CLUE-GPT2](./doc/clue-gpt2-loss-n-acc.png)

### Installation
```bash
pip install .
```

Note that PatrickStar requires gcc of version 7 or higher. You could also use NVIDIA NGC images, the following image is tested:

```bash
docker pull nvcr.io/nvidia/pytorch:21.06-py3
```

### Usage
PatrickStar is based on PyTorch, which makes it easy to migrate a pytorch project. Here is a example of PatrickStar:

```python
from patrickstar.runtime import initialize_engine

config = {
    "optimizer": {
        "type": "Adam",
        "params": {
            "lr": 0.001,
            "betas": (0.9, 0.999),
            "eps": 1e-6,
            "weight_decay": 0,
            "use_hybrid_adam": True,
        },
    },
    "fp16": {  # loss scaler params
        "enabled": True,
        "loss_scale": 0,
        "initial_scale_power": 2 ** 3,
        "loss_scale_window": 1000,
        "hysteresis": 2,
        "min_loss_scale": 1,
    },
    "default_chunk_size": 64 * 1024 * 1024,
    "release_after_init": True,
    "use_cpu_embedding": False,
}

def model_func():
    # MyModel is a derived class for torch.nn.Module
    return MyModel(...)

model, optimizer = initialize_engine(model_func=model_func, local_rank=0, config=config)

...

for data in dataloader:
    optimizer.zero_grad()

    loss = model(data)
    model.backward(loss)
    optimizer.step()
```

We use the same `config` format as [DeepSpeed configuration JSON](https://www.deepspeed.ai/docs/config-json/#optimizer-parameters), which mainly includes params of optimizer, loss scaler and some PatrickStar specific configuration.

For some detail explanation of the above example, please check the guide [here](./GUIDE.md)

For more examples, please check [here](./examples).

### Inside PatrickStar

See [this doc](./INSIDE.md) for the idea behind PatrickStar.

### License
BSD 3-Clause License

### Cite Us
```
@article{fang2021patrickstar,
  title={PatrickStar: Parallel Training of Pre-trained Models via a Chunk-based Memory Management},
  author={Fang, Jiarui and Yu, Yang and Zhu, Zilin and Li, Shenggui and You, Yang and Zhou, Jie},
  journal={arXiv preprint arXiv:2108.05818},
  year={2021}
}
```

### Contact Us
{jiaruifang, zilinzhu, josephyu}@tencent.com

Powered by WeChat AI Team, Tencent NLP Oteam.
