# Standard Library
import asyncio
from asyncio.exceptions import TimeoutError
import json
import logging
import sys
import time
from io import StringIO

# Third Party
from google.protobuf import json_format
import pandas as pd
import payload_pb2
from drain3.template_miner import TemplateMiner
from opni_nats import NatsWrapper

pd.set_option("mode.chained_assignment", None)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
cp_template_miner = TemplateMiner()

nw = NatsWrapper()

def get_serialized_protobuf_object(logs_dict_list):
    payload_list = payload_pb2.PayloadList()
    protobuf_logs = {"items": logs_dict_list}
    return (json_format.ParseDict(protobuf_logs, payload_list)).SerializeToString()

async def load_pretrain_model():
    # This function will load the pretrained DRAIN model for control plane logs in addition to the anomaly level for each template.
    try:
        cp_template_miner.load_state("drain3_control_plane_model_v0.4.1.bin")
        logging.info("Able to load the DRAIN control plane model with {} clusters.".format(cp_template_miner.drain.clusters_counter))
        return True
    except Exception as e:
        logging.error(f"Unable to load DRAIN model {e}")
        return False

async def consume_logs(incoming_cp_logs_queue):
    # This function will subscribe to the Nats subjects preprocessed_logs_control_plane and anomalies.
    async def subscribe_handler(msg):
        payload_data = msg.data
        log_payload_list = payload_pb2.PayloadList()
        logs_df = pd.DataFrame(json_format.MessageToDict(log_payload_list.FromString(payload_data))["items"])
        await incoming_cp_logs_queue.put(logs_df)

    await nw.subscribe(
        nats_subject="preprocessed_logs_pretrained_model",
        nats_queue="workers",
        payload_queue=incoming_cp_logs_queue,
        subscribe_handler=subscribe_handler,
    )

async def inference_logs(incoming_logs_queue):
    '''
        This function will be inferencing on logs which are sent over through Nats and using the DRAIN model to match the logs to a template.
        If no match is made, the log is then sent over to be inferenced on by the Deep Learning model.
    '''
    last_time = time.time()
    logs_inferenced_results = []
    while True:
        logs_df = await incoming_logs_queue.get()
        start_time = time.time()
        logging.info("Received payload of size {}".format(len(logs_df)))
        cp_model_logs = []
        rancher_model_logs = []
        for index, row in logs_df.iterrows():
            log_message = row["maskedLog"]
            if log_message:
                row_dict = row.to_dict()
                template = cp_template_miner.match(log_message)
                if template:
                    row_dict["anomalyLevel"] = template.get_anomaly_level()
                    row_dict["drainPretrainedTemplateMatched"] = template.get_template()
                    row_dict["inferenceModel"] = "drain"
                    logs_inferenced_results.append(row_dict)
                else:
                    if row["logType"] == "controlplane":
                        cp_model_logs.append(row_dict)
                    elif row["logType"] == "rancher":
                        rancher_model_logs.append(row_dict)
        if (start_time - last_time >= 1 and len(logs_inferenced_results) > 0) or len(logs_inferenced_results) >= 128:
            await nw.publish("inferenced_logs", get_serialized_protobuf_object(logs_inferenced_results))
            logs_inferenced_results = []
            last_time = start_time
        if len(cp_model_logs) > 0:
            await nw.publish("opnilog_cp_logs", get_serialized_protobuf_object(cp_model_logs))
            logging.info(f"Published {len(cp_model_logs)} logs to be inferenced on by Control Plane Deep Learning model.")
        if len(rancher_model_logs) > 0:
            await nw.publish("opnilog_rancher_logs", get_serialized_protobuf_object(rancher_model_logs))
            logging.info(f"Published {len(rancher_model_logs)} logs to be inferenced on by Rancher Deep Learning model.")
        logging.info(f"{len(logs_df)} logs processed in {(time.time() - start_time)} second(s)")


async def init_nats():
    # This function initialized the connection to Nats.
    logging.info("connecting to nats")
    await nw.connect()




def main():
    loop = asyncio.get_event_loop()
    incoming_cp_logs_queue = asyncio.Queue(loop=loop)
    init_nats_task = loop.create_task(init_nats())
    loop.run_until_complete(init_nats_task)

    init_model_task = loop.create_task(load_pretrain_model())
    model_loaded = loop.run_until_complete(init_model_task)
    if not model_loaded:
        sys.exit(1)

    preprocessed_logs_consumer_coroutine = consume_logs(incoming_cp_logs_queue)

    match_cp_logs_coroutine = inference_logs(incoming_cp_logs_queue)

    loop.run_until_complete(
        asyncio.gather(
            preprocessed_logs_consumer_coroutine,
            match_cp_logs_coroutine
        )
    )
    try:
        loop.run_forever()
    finally:
        loop.close()