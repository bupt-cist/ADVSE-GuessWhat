import numpy as np
import copy
from generic.tf_utils.optimizer import AccOptimizer
from generic.data_provider.iterator import BasicIterator
from generic.tf_utils.evaluator import Evaluator, EvaluatorC, EvaluatorCR
import numpy as np


class QGenWrapper(object):

    def __init__(self, qgen, batchifier, tokenizer, max_length, k_best):

        self.qgen = qgen

        self.batchifier = batchifier
        self.tokenizer = tokenizer

        self.ops = dict()
        self.ops["sampling"] = qgen.create_sampling_graph(start_token=tokenizer.start_token,
                                                          stop_token=tokenizer.stop_token,
                                                          max_tokens=max_length)

        self.ops["greedy"] = qgen.create_greedy_graph(start_token=tokenizer.start_token,
                                                      stop_token=tokenizer.stop_token,
                                                      max_tokens=max_length)

        beam_predicted_ids, seq_length, att = qgen.create_beam_graph(start_token=tokenizer.start_token,
                                                  stop_token=tokenizer.stop_token,
                                                  max_tokens=max_length,
                                                  k_best=k_best)
        # print('b',beam_predicted_ids)
        # print('s',seq_length)
        # Only keep best beam
        self.ops["beam"] = beam_predicted_ids[:, 0, :], seq_length[:, 0], beam_predicted_ids[:, 0, :] * 0, att

        self.evaluator = None

    def initialize(self, sess):
        self.evaluator = Evaluator(self.qgen.get_sources(sess), self.qgen.scope_name,
                                   network=self.qgen, tokenizer=self.tokenizer)

    def policy_update(self, sess, games, optimizer):

        # ugly hack... to allow training on RL
        batchifier = copy.copy(self.batchifier)
        batchifier.generate = False
        batchifier.supervised = False

        iterator = BasicIterator(games, batch_size=len(games), batchifier=batchifier)

        # Check whether the gradient is accumulated
        if isinstance(optimizer, AccOptimizer):
            sess.run(optimizer.zero)  # reset gradient
            local_optimizer = optimizer.accumulate
        else:
            local_optimizer = optimizer

        # Compute the gradient
        self.evaluator.process(sess, iterator, outputs=[local_optimizer], show_progress=False)

        if isinstance(optimizer, AccOptimizer):
            sess.run(optimizer.update)  # Apply accumulated gradient

    def sample_next_question(self, sess, games, att_dict, beta_dict, mode):

        # ugly hack... to allow training on RL
        batchifier = copy.copy(self.batchifier)
        batchifier.generate = True
        batchifier.supervised = False

        # create the training batch
        batch = batchifier.apply(games, skip_targets=True)
        batch["is_training"] = False
        batch["is_dynamic"] = True

        # Sample
        tokens, seq_length, state_values, atts = self.evaluator.execute(sess, output=self.ops[mode], batch=batch)
        # tokens, seq_length, state_values, atts, betas = self.evaluator.execute(sess, output=self.ops[mode], batch=batch)

        # Update game
        new_games = []
        for game, question_tokens, l, state_value, att in zip(games, tokens, seq_length, state_values, atts):
        # for game, question_tokens, l, state_value, att, beta in zip(games, tokens, seq_length, state_values, atts, betas):

            if not game.user_data["has_stop_token"]:  # stop adding question if dialogue is over

                # clean tokens after stop_dialogue_tokens
                if self.tokenizer.stop_dialogue in question_tokens:
                    game.user_data["has_stop_token"] = True
                    l = np.nonzero(question_tokens == self.tokenizer.stop_dialogue)[0][0] + 1  # find the first stop_dialogue occurrence
                # Append the newly generated question
                game.questions.append(self.tokenizer.decode(question_tokens[:l]))
                game.question_ids.append(len(game.question_ids))

                game.user_data["state_values"] = game.user_data.get("state_values", [])
                game.user_data["state_values"].append(state_value[:l].tolist())
            att = att.tolist()
            att_i = np.argsort(att).tolist()
            att_3 = np.sort(att).tolist()
            if game.dialogue_id not in att_dict:
                att_dict[game.dialogue_id] = []
                att_dict[game.dialogue_id].append((att_i, att_3))
            else:
                att_dict[game.dialogue_id].append((att_i, att_3))

            # beta = beta.tolist()
            # if game.dialogue_id not in beta_dict:
            #     beta_dict[game.dialogue_id] = []
            #     beta_dict[game.dialogue_id].append(beta)
            # else:
            #     beta_dict[game.dialogue_id].append(beta)

            new_games.append(game)

        return new_games, att_dict  #, beta_dict


class QGenWrapperC(object):

    def __init__(self, qgen, batchifier, tokenizer, max_length, k_best):

        self.qgen = qgen

        self.batchifier = batchifier
        self.tokenizer = tokenizer

        self.ops = dict()
        self.ops["sampling"] = qgen.create_sampling_graph(start_token=tokenizer.start_token,
                                                          stop_token=tokenizer.stop_token,
                                                          max_tokens=max_length)

        self.ops["greedy"] = qgen.create_greedy_graph(start_token=tokenizer.start_token,
                                                      stop_token=tokenizer.stop_token,
                                                      max_tokens=max_length)

        beam_predicted_ids, seq_length, att = qgen.create_beam_graph(start_token=tokenizer.start_token,
                                                                     stop_token=tokenizer.stop_token,
                                                                     max_tokens=max_length,
                                                                     k_best=k_best)
        # print('b',beam_predicted_ids)
        # print('s',seq_length)
        # Only keep best beam
        self.ops["beam"] = beam_predicted_ids[:, 0, :], seq_length[:, 0], beam_predicted_ids[:, 0, :] * 0, att

        self.evaluator = None

    def initialize(self, sess):
        self.evaluator = EvaluatorC(self.qgen.get_sources(sess), self.qgen.scope_name,
                                   network=self.qgen, tokenizer=self.tokenizer)

    def policy_update(self, sess, games, distri, q_flag, optimizer):

        # ugly hack... to allow training on RL
        batchifier = copy.copy(self.batchifier)
        batchifier.generate = False
        batchifier.supervised = False

        iterator = BasicIterator(games, batch_size=len(games), batchifier=batchifier)

        # Check whether the gradient is accumulated
        if isinstance(optimizer, AccOptimizer):
            sess.run(optimizer.zero)  # reset gradient
            local_optimizer = optimizer.accumulate
        else:
            local_optimizer = optimizer

        # Compute the gradient
        self.evaluator.process(sess, iterator, distri, outputs=[local_optimizer], show_progress=False)

        if isinstance(optimizer, AccOptimizer):
            sess.run(optimizer.update)  # Apply accumulated gradient

    def sample_next_question(self, sess, games, att_dict, beta_dict, mode):

        # ugly hack... to allow training on RL
        batchifier = copy.copy(self.batchifier)
        batchifier.generate = True
        batchifier.supervised = False

        # create the training batch
        batch = batchifier.apply(games, skip_targets=True)
        batch["is_training"] = False
        batch["is_dynamic"] = True

        # Sample
        tokens, seq_length, state_values, atts = self.evaluator.execute(sess, output=self.ops[mode], batch=batch)
        # tokens, seq_length, state_values, atts, betas = self.evaluator.execute(sess, output=self.ops[mode], batch=batch)

        # Update game
        new_games = []
        for game, question_tokens, l, state_value, att in zip(games, tokens, seq_length, state_values, atts):
            # for game, question_tokens, l, state_value, att, beta in zip(games, tokens, seq_length, state_values, atts, betas):

            if not game.user_data["has_stop_token"]:  # stop adding question if dialogue is over

                # clean tokens after stop_dialogue_tokens
                if self.tokenizer.stop_dialogue in question_tokens:
                    game.user_data["has_stop_token"] = True
                    l = np.nonzero(question_tokens == self.tokenizer.stop_dialogue)[0][
                            0] + 1  # find the first stop_dialogue occurrence
                # Append the newly generated question
                game.questions.append(self.tokenizer.decode(question_tokens[:l]))
                game.question_ids.append(len(game.question_ids))

                game.user_data["state_values"] = game.user_data.get("state_values", [])
                game.user_data["state_values"].append(state_value[:l].tolist())
            att = att.tolist()
            att_i = np.argsort(att).tolist()
            att_3 = np.sort(att).tolist()
            if game.dialogue_id not in att_dict:
                att_dict[game.dialogue_id] = []
                att_dict[game.dialogue_id].append((att_i, att_3))
            else:
                att_dict[game.dialogue_id].append((att_i, att_3))

            # beta = beta.tolist()
            # if game.dialogue_id not in beta_dict:
            #     beta_dict[game.dialogue_id] = []
            #     beta_dict[game.dialogue_id].append(beta)
            # else:
            #     beta_dict[game.dialogue_id].append(beta)

            new_games.append(game)

        return new_games, att_dict  # , beta_dict


class QGenWrapperCR(object):

    def __init__(self, qgen, batchifier, tokenizer, max_length, k_best):

        self.qgen = qgen

        self.batchifier = batchifier
        self.tokenizer = tokenizer

        self.ops = dict()
        self.ops["sampling"] = qgen.create_sampling_graph(start_token=tokenizer.start_token,
                                                          stop_token=tokenizer.stop_token,
                                                          max_tokens=max_length)

        self.ops["greedy"] = qgen.create_greedy_graph(start_token=tokenizer.start_token,
                                                      stop_token=tokenizer.stop_token,
                                                      max_tokens=max_length)

        beam_predicted_ids, seq_length, att = qgen.create_beam_graph(start_token=tokenizer.start_token,
                                                                     stop_token=tokenizer.stop_token,
                                                                     max_tokens=max_length,
                                                                     k_best=k_best)
        # print('b',beam_predicted_ids)
        # print('s',seq_length)
        # Only keep best beam
        self.ops["beam"] = beam_predicted_ids[:, 0, :], seq_length[:, 0], beam_predicted_ids[:, 0, :] * 0, att

        self.evaluator = None

    def initialize(self, sess):
        self.evaluator = EvaluatorCR(self.qgen.get_sources(sess), self.qgen.scope_name,
                                    network=self.qgen, tokenizer=self.tokenizer)

    def policy_update(self, sess, games, distri, q_flag, optimizer):

        # ugly hack... to allow training on RL
        batchifier = copy.copy(self.batchifier)
        batchifier.generate = False
        batchifier.supervised = False

        iterator = BasicIterator(games, batch_size=len(games), batchifier=batchifier)

        # Check whether the gradient is accumulated
        if isinstance(optimizer, AccOptimizer):
            sess.run(optimizer.zero)  # reset gradient
            local_optimizer = optimizer.accumulate
        else:
            local_optimizer = optimizer

        # Compute the gradient
        self.evaluator.process(sess, iterator, distri, q_flag, outputs=[local_optimizer], show_progress=False)

        if isinstance(optimizer, AccOptimizer):
            sess.run(optimizer.update)  # Apply accumulated gradient

    def sample_next_question(self, sess, games, att_dict, beta_dict, mode):

        # ugly hack... to allow training on RL
        batchifier = copy.copy(self.batchifier)
        batchifier.generate = True
        batchifier.supervised = False

        # create the training batch
        batch = batchifier.apply(games, skip_targets=True)
        batch["is_training"] = False
        batch["is_dynamic"] = True

        # Sample
        tokens, seq_length, state_values, atts = self.evaluator.execute(sess, output=self.ops[mode], batch=batch)
        # tokens, seq_length, state_values, atts, betas = self.evaluator.execute(sess, output=self.ops[mode], batch=batch)

        # Update game
        new_games = []
        q_flag = []
        for game, question_tokens, l, state_value, att in zip(games, tokens, seq_length, state_values, atts):
            # for game, question_tokens, l, state_value, att, beta in zip(games, tokens, seq_length, state_values, atts, betas):

            if not game.user_data["has_stop_token"]:  # stop adding question if dialogue is over

                # clean tokens after stop_dialogue_tokens
                if self.tokenizer.stop_dialogue in question_tokens:
                    game.user_data["has_stop_token"] = True
                    l = np.nonzero(question_tokens == self.tokenizer.stop_dialogue)[0][
                            0] + 1  # find the first stop_dialogue occurrence
                # Append the newly generated question
                if self.tokenizer.decode(question_tokens[:l]) in game.questions:
                    q_flag.append(0)
                else:
                    q_flag.append(1)
                game.questions.append(self.tokenizer.decode(question_tokens[:l]))
                game.question_ids.append(len(game.question_ids))

                game.user_data["state_values"] = game.user_data.get("state_values", [])
                game.user_data["state_values"].append(state_value[:l].tolist())
            att = att.tolist()
            att_i = np.argsort(att).tolist()
            att_3 = np.sort(att).tolist()
            if game.dialogue_id not in att_dict:
                att_dict[game.dialogue_id] = []
                att_dict[game.dialogue_id].append((att_i, att_3))
            else:
                att_dict[game.dialogue_id].append((att_i, att_3))

            # beta = beta.tolist()
            # if game.dialogue_id not in beta_dict:
            #     beta_dict[game.dialogue_id] = []
            #     beta_dict[game.dialogue_id].append(beta)
            # else:
            #     beta_dict[game.dialogue_id].append(beta)

            new_games.append(game)

        return new_games, att_dict, q_flag  # , beta_dict

# TODO: refactor
# class QGenUserWrapper(object):
#     def __init__(self, tokenizer):
#         self.tokenizer = tokenizer
#
#     def initialize(self, sess):
#         pass
#
#     def reset(self, batch_size):
#         pass
#
#     def sample_next_question(self, _, prev_answers, game_data, **__):
#
#         if prev_answers[0] == self.tokenizer.start_token:
#             print("Type the character '(S)top' when you want to guess the object")
#         else:
#             print("A :", self.tokenizer.decode(prev_answers[0]))
#
#         print()
#         while True:
#             question = input('Q: ')
#             if question != "":
#                 break
#
#         # Stop the dialogue
#         if question == "S" or question == "Stop":
#             tokens = [self.tokenizer.stop_dialogue]
#
#         # Stop the question (add stop token)
#         else:
#             question = re.sub('\?', '', question) # remove question tags if exist
#             question +=  " ?"
#             tokens = self.tokenizer.apply(question)
#
#         return [tokens], np.array([tokens]), [len(tokens)]
