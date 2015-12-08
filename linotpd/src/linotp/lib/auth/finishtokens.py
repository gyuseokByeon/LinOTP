# -*- coding: utf-8 -*-
#
#    LinOTP - the open source solution for two factor authentication
#    Copyright (C) 2010 - 2015 LSE Leading Security Experts GmbH
#
#    This file is part of LinOTP server.
#
#    This program is free software: you can redistribute it and/or
#    modify it under the terms of the GNU Affero General Public
#    License, version 3, as published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the
#               GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
#    E-mail: linotp@lsexperts.de
#    Contact: www.linotp.org
#    Support: www.lsexperts.de
#

from pylons import tmpl_context as c

from linotp.lib.auth.validate import log
from linotp.lib.challenges import Challenges
from linotp.lib.error import UserError


class FinishTokens(object):
    def __init__(self, valid_tokens, challenge_tokens, pin_matching_tokens,
                 invalid_tokens, validation_results, user, options,
                 context=None):
        """
        create the finalisation object, that finishes the token processing

        :param valid_tokens: list of valid tokens
        :param challenge_tokens: list of the tokens, that trigger a challenge
        :param pin_matching_tokens: list of tokens with a matching pin
        :param invalid_tokens: list of the invalid tokens
        :param validation_results: dict of the verification response
        :param user: the requesting user
        :param options: request options - additional parameters
        """

        self.valid_tokens = valid_tokens
        self.challenge_tokens = challenge_tokens
        self.pin_matching_tokens = pin_matching_tokens
        self.invalid_tokens = invalid_tokens
        self.validation_results = validation_results
        self.user = user
        self.options = options
        self.context = context

    def finish_checked_tokens(self):
        """
        main entry to finalise the involved tokens
        """

        # do we have any valid tokens?
        if self.valid_tokens:
            (ret, reply, detail) = self.finish_valid_tokens()
            self.reset_failcounter(self.valid_tokens +
                                   self.invalid_tokens +
                                   self.pin_matching_tokens +
                                   self.challenge_tokens)

            self.create_audit_entry(detail, self.valid_tokens)
            return ret, reply

        # next handle the challenges
        if self.challenge_tokens:
            (ret, reply, detail) = self.finish_challenge_token()
            # do we have to increment the counter to prevent a replay???
            # self.increment_counters(self.challenge_tokens)
            self.create_audit_entry(detail, self.challenge_tokens)
            return ret, reply

        if self.user:
            log.warning("user %r@%r failed to auth."
                        % (self.user.login, self.user.realm))
        else:
            log.warning("serial %r failed to auth."
                        % (self.pin_matching_tokens +
                           self.invalid_tokens)[0].getSerial())

        if self.pin_matching_tokens:
            (ret, reply, detail) = self.finish_pin_matching_tokens()
            # in case of pin matching, we have to treat as well the invalid
            self.increment_failcounters(self.pin_matching_tokens)
            self.finish_invalid_tokens()

            # check for the global settings, if we increment in wrong pin
            inc_on_false_pin = self.context.get(
                "linotp.FailCounterIncOnFalsePin", "True")
            if inc_on_false_pin.strip().lower() == 'true':
                self.increment_failcounters(self.invalid_tokens)
            self.create_audit_entry(detail, self.pin_matching_tokens)
            return ret, reply

        if self.invalid_tokens:
            (ret, reply, detail) = self.finish_invalid_tokens()
            self.increment_failcounters(self.invalid_tokens)

            self.create_audit_entry(detail, self.invalid_tokens)
            return ret, reply

        # if there is no token left, we hend up here
        self.create_audit_entry("no token found", [])
        return False, None

    def finish_valid_tokens(self):
        """
        processing of the valid tokens
        """
        valid_tokens = self.valid_tokens
        validation_results = self.validation_results
        user = self.user

        if len(valid_tokens) == 1:
            token = valid_tokens[0]
            if user:
                action_detail = ("user %r@%r successfully authenticated."
                                 % (user.login, user.realm))
            else:
                action_detail = ("serial %r successfully authenticated."
                                 % token.getSerial())

            log.info(action_detail)

            # there could be a match in the window ahead,
            # so we need the last valid counter here
            (counter, _reply) = validation_results[token.getSerial()]
            token.setOtpCount(counter + 1)
            token.statusValidationSuccess()
            if token.getFromTokenInfo('count_auth_success_max', default=None):
                auth_count = token.get_count_auth_success()
                token.set_count_auth_success(auth_count + 1)
            return (True, None, action_detail)

        else:
            # we have to set the matching counter to prevent replay one one
            # single token
            for token in valid_tokens:
                (res, _reply) = validation_results[token.getSerial()]
                token.setOtpCount(res)

            self.context['audit']['action_detail'] = "Multiple valid tokens found!"
            if user:
                log.error("[__checkTokenList] multiple token match error: "
                          "Several Tokens matching with the same OTP PIN "
                          "and OTP for user %r. Not sure how to auth",
                          user.login)
            raise UserError("multiple token match error", id=-33)

    def finish_challenge_token(self):
        """
        processing of the challenge tokens
        """
        challenge_tokens = self.challenge_tokens
        options = self.options
        if not options:
            options = {}

        action_detail = 'challenge created'

        if len(challenge_tokens) == 1:
            challenge_token = challenge_tokens[0]
            _res, reply = Challenges.create_challenge(
                                challenge_token, self.context, options=options)
            return (False, reply, action_detail)

        # processing of multiple challenges
        else:
            # for each token, who can submit a challenge, we have to
            # create the challenge. To mark the challenges as depending
            # the transaction id will have an id that all sub transaction share
            # and a postfix with their enumaration. Finally the result is
            # composed by the top level transaction id and the message
            # and below in a dict for each token a challenge description -
            # the key is the token type combined with its token serial number
            all_reply = {'challenges': {}}
            challenge_count = 0
            transactionid = ''
            challenge_id = ""
            for challenge_token in challenge_tokens:
                challenge_count += 1
                id_postfix = ".%02d" % challenge_count
                if transactionid:
                    challenge_id = "%s%s" % (transactionid, id_postfix)

                (_res, reply) = Challenges.create_challenge(
                    challenge_token,
                    self.context,
                    options=options,
                    challenge_id=challenge_id,
                    id_postfix=id_postfix
                )
                transactionid = reply.get('transactionid').rsplit('.')[0]

                # add token type and serial to ease the type specific processing
                reply['linotp_tokentype'] = challenge_token.type
                reply['linotp_tokenserial'] = challenge_token.getSerial()
                key = challenge_token.getSerial()
                all_reply['challenges'][key] = reply

            # finally add the root challenge response with top transaction id
            # and message, that indicates that 'multiple challenges have been
            # submitted
            all_reply['transactionid'] = transactionid
            all_reply['message'] = "Multiple challenges submitted."

            log.debug("Multiple challenges submitted: %d",
                      len(challenge_tokens))

            return (False, all_reply, action_detail)

    def finish_pin_matching_tokens(self):
        """
            check, if there have been some tokens
            where the pin matched (but OTP failed
            and increment only these
        """
        pin_matching_tokens = self.pin_matching_tokens
        action_detail = "wrong otp value"

        for tok in pin_matching_tokens:
            tok.statusValidationFail()
            tok.inc_count_auth()

        return (False, None, action_detail)

    def finish_invalid_tokens(self):
        """
        """
        invalid_tokens = self.invalid_tokens
        user = self.user

        for tok in invalid_tokens:
            tok.statusValidationFail()

        import linotp.lib.policy
        pin_policies = linotp.lib.policy.get_pin_policies(
                                            user, context=self.context) or []

        if 1 in pin_policies:
            action_detail = "wrong user password -1"
        else:
            action_detail = "wrong otp pin -1"

        return (False, None, action_detail)

    @staticmethod
    def reset_failcounter(all_tokens):
        for token in all_tokens:
            token.reset()

    @staticmethod
    def increment_counters(all_tokens, reset=True):
        for token in all_tokens:
            token.incOtpCounter(reset=reset)

    @staticmethod
    def increment_failcounters(all_tokens):
        for token in all_tokens:
            token.incOtpFailCounter()

    @staticmethod
    def create_audit_entry(action_detail, tokens):
        """
        setting global audit entry

        :param tokens:
        :param action_detail:
        """

        c.audit['action_detail'] = action_detail

        if len(tokens) == 1:
            c.audit['serial'] = tokens[0].getSerial()
            c.audit['token_type'] = tokens[0].getType()
        else:
            # no or multiple tokens
            serials = []
            types = []
            for token in tokens:
                serials.append(token.getSerial())
                types.append(token.getType())
            c.audit['serial'] = ' '.join(serials)[:29]
            c.audit['token_type'] = ' '.join(types)[:39]

        return
# eof###########################################################################
